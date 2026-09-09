"""Stage 1: a pinned OSM extract in, candidate polylines out.

    uv run --directory derive python -m krpaly_derive.extract \
        --pbf ~/data/czech-republic-260901.osm.pbf --out data/kraj-1

Reads a local `.pbf` — it does not download one; fetching 945 MB is a `curl`
the operator runs, and wrapping it would put a network path inside a batch
stage and inside its tests. What comes out is `candidates.parquet`, one row
per stretch of cyclable road between two decision points, in both directions,
each carrying the OSM way ids and node ids that made it.

Two properties are the reason this stage exists as its own file on disk
rather than a pipe into #8:

* `way_refs` is the anchor. Identity is the ordered way ids plus the start
  and end node — a fact about the world rather than about the detector — and
  no later stage can invent it.
* Both directions are emitted. A climb one way is a descent the other, and
  two sides of one summit are two climbs; skip it here and half the database
  is missing in a way no later stage recovers.

Re-running is cheap and re-entrant: DEM sampling is the slow stage and will
be re-run for reasons unrelated to OSM, so an unchanged input leaves the
output alone.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
import time
from collections import Counter
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

import osmium
import pyarrow as pa
import pyarrow.parquet as pq
import shapely
from pyproj import Transformer
from shapely.ops import transform as reproject
from shapely.prepared import PreparedGeometry, prep

from krpaly_derive.cyclable import PREDICATE_VERSION, is_cyclable

# Moravskoslezský kraj. A default rather than a constant because kraj-2 is
# the same command with a different number — see derive/INPUTS.md § Kraj
# boundary.
DEFAULT_BOUNDARY_RELATION = 442461

# 10 km comfortably exceeds the longest plausible Czech climb, so a climb
# that tops out across the border is extracted whole and assigned to a kraj
# by its summit later. Nothing is truncated at the boundary.
DEFAULT_BUFFER_M = 10_000.0

# libosmium's node-location cache. flex_mem is right up to a country-sized
# extract; a machine short of RAM passes sparse_file_array.
DEFAULT_INDEX = "flex_mem"

OUTPUT_NAME = "candidates.parquet"
MANIFEST_NAME = "candidates.manifest.json"

# S-JTSK / Krovak East North: the national projected CRS, metres, and the one
# in which a 10 km buffer is 10 km. Buffering in degrees would be 10 km
# north-south and about 6.5 km east-west at Czech latitudes.
PROJECTED_CRS = "EPSG:5514"

# Rows per Parquet row group. Fixed rather than left to pyarrow's default so
# that the same rows produce the same bytes: "delete the output and re-run
# produces the identical file" is a done-when of this stage, and the test
# asserts the sha256.
ROW_GROUP_SIZE = 50_000

SCHEMA = pa.schema(
    [
        # Index in emission order, not a hash: two runs over the same input
        # emit the same rows in the same order, so an id that is just the
        # position is stable and says where to look.
        ("candidate_id", pa.uint64()),
        # The anchor. One element at this stage — splitting happens *within*
        # a way — but the column is a list because a climb spans several
        # segments and therefore several ways, and #9/#10 concatenate in
        # order.
        ("way_refs", pa.list_(pa.int64())),
        ("node_ids", pa.list_(pa.int64())),
        ("start_node_id", pa.int64()),
        ("end_node_id", pa.int64()),
        ("direction", pa.string()),
        ("geometry", pa.binary()),
        # Equals len(node_ids); the same off-by-one climb_profile guards.
        ("n_points", pa.int32()),
    ]
)

# GeoParquet 1.1. `crs: null` is the specification's own way of saying
# OGC:CRS84, and it is preferable here to an embedded PROJJSON document,
# which would make the file's bytes a function of the installed PROJ.
GEO_METADATA = {
    "version": "1.1.0",
    "primary_column": "geometry",
    "columns": {
        "geometry": {
            "encoding": "WKB",
            "geometry_types": ["LineString"],
            "crs": None,
        }
    },
}

FORWARD = "forward"
REVERSE = "reverse"


class ExtractError(Exception):
    """An input this stage cannot derive from, said in one line."""


@dataclass(frozen=True)
class Boundary:
    """The kraj as the pinned extract has it, buffered and ready to test against."""

    relation_version: int
    bounds: tuple[float, float, float, float]
    prepared: PreparedGeometry

    def contains_any(self, min_lon, min_lat, max_lon, max_lat, line) -> bool:
        """Bbox reject first, then the real test.

        The bbox comparison is a handful of float compares and rejects the
        overwhelming majority of a country's ways when the target is one
        kraj; `prepared.intersects` is the expensive one and runs only on
        what survives.
        """
        west, south, east, north = self.bounds
        if max_lon < west or min_lon > east or max_lat < south or min_lat > north:
            return False
        return self.prepared.intersects(line)


@dataclass
class Counts:
    """What the run saw, for the manifest.

    Drops are counted by reason rather than summed away: a candidate that
    vanishes for the wrong reason is invisible in a climb count, and the
    numbers here are the only place it would show.
    """

    ways_seen: int = 0
    ways_cyclable: int = 0
    segments: int = 0
    segments_degenerate: int = 0
    segments_missing_location: int = 0
    segments_outside_buffer: int = 0

    def as_dict(self) -> dict[str, int]:
        dropped = (
            self.segments_degenerate + self.segments_missing_location + self.segments_outside_buffer
        )
        return {
            "ways_seen": self.ways_seen,
            "ways_cyclable": self.ways_cyclable,
            "segments": self.segments,
            "candidates": 2 * self.segments,
            "segments_degenerate": self.segments_degenerate,
            "segments_missing_location": self.segments_missing_location,
            "segments_outside_buffer": self.segments_outside_buffer,
            "segments_dropped": dropped,
        }


def sha256_of(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1 << 20), b""):
            digest.update(block)
    return digest.hexdigest()


def read_header(pbf: Path) -> dict[str, str | None]:
    """What the PBF header says about the snapshot it holds.

    The replication timestamp is the fact about the data, and it lives inside
    the file rather than in its name — see derive/INPUTS.md § OSM snapshot.
    An absent field reads as None rather than as an empty string, so a
    manifest never claims a value the file did not carry.

    libosmium exposes the header's `writingprogram` field under the name
    `generator`, which is why that key is read and reported under the name
    the file format uses.
    """
    reader = osmium.io.Reader(str(pbf))
    try:
        header = reader.header()
    finally:
        reader.close()

    def get(key: str) -> str | None:
        return header.get(key) or None

    return {
        "replication_ts": get("osmosis_replication_timestamp"),
        "replication_seq": get("osmosis_replication_sequence_number"),
        "replication_base_url": get("osmosis_replication_base_url"),
        "writingprogram": get("generator"),
    }


def read_boundary(pbf: Path, relation_id: int, buffer_m: float, index: str) -> Boundary:
    """Pass 1: the boundary relation's version *and* its geometry, buffered.

    The version wanted is the one in the extract, never the live one: the
    derivation reads the snapshot, and a climb count that moved because the
    boundary moved is exactly what this records.

    libosmium's multipolygon manager accepts `type=boundary` as well as
    `type=multipolygon`, so the kraj assembles into an `Area` like any other.
    The id filter is what keeps that affordable — without it the manager
    would assemble every multipolygon in Czechia to find one.
    """
    wkb_factory = osmium.geom.WKBFactory()
    version: int | None = None
    geometry = None

    processor = (
        osmium.FileProcessor(str(pbf))
        .with_locations(index)
        .with_areas(osmium.filter.IdFilter([relation_id]).enable_for(osmium.osm.RELATION))
        .with_filter(osmium.filter.EntityFilter(osmium.osm.RELATION | osmium.osm.AREA))
    )
    for obj in processor:
        if isinstance(obj, osmium.osm.Area):
            if not obj.from_way() and obj.orig_id() == relation_id:
                geometry = shapely.from_wkb(bytes.fromhex(wkb_factory.create_multipolygon(obj)))
        elif obj.id == relation_id:
            version = obj.version

    # Both halves are fatal, and separately: a relation that is absent is a
    # mistyped number, one that does not assemble is a broken extract, and a
    # silently empty boundary yields a silently empty output either way.
    if version is None:
        raise ExtractError(f"relation {relation_id} is not in {pbf} — check --boundary-relation")
    if geometry is None or geometry.is_empty:
        raise ExtractError(
            f"relation {relation_id} is in {pbf} but does not assemble into an area — "
            "its ways are incomplete or it is not a closed boundary"
        )

    to_metres = Transformer.from_crs("OGC:CRS84", PROJECTED_CRS, always_xy=True)
    to_degrees = Transformer.from_crs(PROJECTED_CRS, "OGC:CRS84", always_xy=True)
    projected = reproject(to_metres.transform, geometry)
    buffered = reproject(to_degrees.transform, projected.buffer(buffer_m))

    return Boundary(relation_version=version, bounds=buffered.bounds, prepared=prep(buffered))


def scan_ways(pbf: Path, counts: Counts) -> tuple[set[int], Counter]:
    """Pass 2: which ways are cyclable, and how often each node is visited.

    Locations are deliberately not built here — this pass only needs ids, and
    it is the cheap one.

    **Degree is counted over all of Czechia, not over the kraj**, and the
    boundary filter is applied after splitting. A junction just outside the
    buffer therefore still splits the way, so the same road yields the same
    segments no matter which kraj is being derived. That is the property that
    makes kraj-2 comparable to kraj-1.

    Occurrences, not distinct ways: a node one way visits twice is a
    self-intersection and a real decision point.
    """
    cyclable: set[int] = set()
    degree: Counter = Counter()

    for way in osmium.FileProcessor(str(pbf), osmium.osm.WAY):
        counts.ways_seen += 1
        if not is_cyclable(way.tags):
            continue
        counts.ways_cyclable += 1
        cyclable.add(way.id)
        degree.update(node.ref for node in way.nodes)

    return cyclable, degree


def cut_indices(node_ids: list[int], degree: Counter) -> list[int]:
    """Where a way becomes several stretches of road.

    The two ends always cut, and so does every interior node that more than
    one cyclable way visit — or that one way visits twice.
    """
    last = len(node_ids) - 1
    interior = [i for i in range(1, last) if degree[node_ids[i]] >= 2]
    return [0, *interior, last]


def emit_ways(
    pbf: Path,
    cyclable: set[int],
    degree: Counter,
    boundary: Boundary,
    index: str,
    counts: Counts,
) -> Iterator[tuple[tuple[int, int, int], dict]]:
    """Pass 3: split the cyclable ways and emit both directions of each piece.

    This is the memory-hungry pass — `with_locations` builds the node index
    for the whole extract — and the reason `--index` exists.

    Each row is yielded with the sort key it will be ordered by, rather than
    trusting the order objects come out of the file in. Emission order is
    ascending way id, then position along the way, then forward before
    reverse; that order is the determinism guarantee, so nothing downstream
    should sort and nothing here should iterate a set.
    """
    # Nodes have to be read for the location cache to fill, and the entity
    # filter then keeps them out of the Python loop: this pass touches every
    # node in Czechia and only the cyclable ways are worth a call frame.
    processor = (
        osmium.FileProcessor(str(pbf), osmium.osm.NODE | osmium.osm.WAY)
        .with_locations(index)
        .with_filter(osmium.filter.EntityFilter(osmium.osm.WAY))
    )
    for way in processor:
        if way.id not in cyclable:
            continue

        node_ids = [node.ref for node in way.nodes]
        if len(node_ids) < 2:
            continue
        cuts = cut_indices(node_ids, degree)

        for seq, (start, end) in enumerate(zip(cuts, cuts[1:], strict=False)):
            piece = node_ids[start : end + 1]

            # A segment between two occurrences of one node has no length to
            # profile. Distinct nodes rather than a length check: the fix for
            # a duplicated node in OSM is upstream, not here.
            if len(set(piece)) < 2:
                counts.segments_degenerate += 1
                continue

            # A way whose nodes are not all in the extract — the usual cause
            # is a way clipped at the download boundary. Reported, never
            # absorbed: a partial geometry is a wrong geometry.
            locations = [way.nodes[i].location for i in range(start, end + 1)]
            if not all(location.valid() for location in locations):
                counts.segments_missing_location += 1
                continue

            coords = [(location.lon, location.lat) for location in locations]
            line = shapely.LineString(coords)
            lons = [lon for lon, _ in coords]
            lats = [lat for _, lat in coords]
            if not boundary.contains_any(min(lons), min(lats), max(lons), max(lats), line):
                counts.segments_outside_buffer += 1
                continue

            counts.segments += 1
            forward_wkb = shapely.to_wkb(line)
            reverse_wkb = shapely.to_wkb(shapely.LineString(coords[::-1]))
            reversed_ids = piece[::-1]

            yield (
                (way.id, seq, 0),
                {
                    "way_refs": [way.id],
                    "node_ids": piece,
                    "start_node_id": piece[0],
                    "end_node_id": piece[-1],
                    "direction": FORWARD,
                    "geometry": forward_wkb,
                    "n_points": len(piece),
                },
            )
            yield (
                (way.id, seq, 1),
                {
                    "way_refs": [way.id],
                    "node_ids": reversed_ids,
                    "start_node_id": reversed_ids[0],
                    "end_node_id": reversed_ids[-1],
                    "direction": REVERSE,
                    "geometry": reverse_wkb,
                    "n_points": len(reversed_ids),
                },
            )


def write_parquet(rows: list[dict], path: Path) -> None:
    """Write the table, atomically, in a fixed shape.

    Through a temporary file and `os.replace` so an interrupted run leaves no
    half file for #8 to read: the next stage checks for the output's
    existence, not its integrity.
    """
    columns = {name: [row[name] for row in rows] for name in SCHEMA.names if name != "candidate_id"}
    columns["candidate_id"] = list(range(len(rows)))
    table = pa.table({name: columns[name] for name in SCHEMA.names}, schema=SCHEMA)
    table = table.replace_schema_metadata({"geo": json.dumps(GEO_METADATA, sort_keys=True)})

    tmp = path.with_suffix(path.suffix + ".tmp")
    pq.write_table(
        table,
        tmp,
        compression="zstd",
        row_group_size=ROW_GROUP_SIZE,
        version="2.6",
    )
    os.replace(tmp, path)


def stage_signature(snapshot: dict, boundary_relation: int, buffer_m: float) -> dict[str, object]:
    """Everything about a run that changes what comes out of it.

    Dotted paths into the manifest rather than whole blocks, because the
    manifest's `boundary` block also carries the relation version, which is
    read *out of* the input rather than given to the run and so cannot be
    known before it starts.
    """
    return {
        "osm_snapshot.sha256": snapshot["sha256"],
        "way_filter.version": PREDICATE_VERSION,
        "boundary.relation_id": boundary_relation,
        "boundary.buffer_m": buffer_m,
    }


def already_done(manifest_path: Path, output_path: Path, signature: dict[str, object]) -> bool:
    """Whether a previous run of this exact stage is sitting in the output directory.

    This is the "resumable" the ticket asks for: re-running #8 must not
    re-run #6. A changed `--buffer-m` or a bumped tag predicate is part of
    the signature, so it re-derives rather than silently reusing a file that
    was built with the other one.
    """
    if not (manifest_path.exists() and output_path.exists()):
        return False
    try:
        prior = json.loads(manifest_path.read_text())
    except (OSError, json.JSONDecodeError):
        # A manifest that cannot be read is a manifest that proves nothing.
        return False
    for path, expected in signature.items():
        block, key = path.split(".")
        if prior.get(block, {}).get(key) != expected:
            return False
    return True


def extract(
    pbf: Path,
    out: Path,
    boundary_relation: int,
    buffer_m: float,
    index: str,
    force: bool,
) -> int:
    if not pbf.is_file():
        raise ExtractError(f"{pbf} is not a file — pass --pbf a Geofabrik .osm.pbf")
    if index not in osmium.index.map_types():
        raise ExtractError(f"{index!r} is not a location index — try {DEFAULT_INDEX}")

    out.mkdir(parents=True, exist_ok=True)
    output_path = out / OUTPUT_NAME
    manifest_path = out / MANIFEST_NAME

    started = time.monotonic()
    started_at = datetime.now(UTC).isoformat(timespec="seconds")

    header = read_header(pbf)
    provenance = {
        "osm_snapshot": {
            "path": str(pbf.resolve()),
            "bytes": pbf.stat().st_size,
            "sha256": sha256_of(pbf),
            **header,
        },
        "way_filter": {"version": PREDICATE_VERSION},
    }

    signature = stage_signature(provenance["osm_snapshot"], boundary_relation, buffer_m)
    if not force and already_done(manifest_path, output_path, signature):
        print(
            f"{output_path} is already derived from this input — pass --force to redo",
            file=sys.stderr,
        )
        return 0

    counts = Counts()
    print(f"boundary: relation {boundary_relation}, {buffer_m:.0f} m buffer", file=sys.stderr)
    boundary = read_boundary(pbf, boundary_relation, buffer_m, index)
    print(f"boundary: version {boundary.relation_version} in the extract", file=sys.stderr)

    cyclable, degree = scan_ways(pbf, counts)
    print(f"ways: {counts.ways_cyclable} cyclable of {counts.ways_seen}", file=sys.stderr)

    emitted = sorted(
        emit_ways(pbf, cyclable, degree, boundary, index, counts), key=lambda item: item[0]
    )
    rows = [row for _key, row in emitted]
    write_parquet(rows, output_path)
    print(f"candidates: {len(rows)} from {counts.segments} segments", file=sys.stderr)

    manifest = {
        **provenance,
        "boundary": {
            "relation_id": boundary_relation,
            "relation_version": boundary.relation_version,
            "buffer_m": buffer_m,
            # A column rather than a constant in the schema too, so a later
            # change of border policy is a visible change of data.
            "assignment": "summit",
        },
        "counts": counts.as_dict(),
        "output": {
            "file": OUTPUT_NAME,
            "sha256": sha256_of(output_path),
            "bytes": output_path.stat().st_size,
        },
        # The only block allowed to differ between two runs over the same
        # input. Separated so re-entrancy can be asserted on the manifest
        # minus this, and so a reader does not have to guess which fields are
        # provenance and which are timing.
        "run": {
            "started_at": started_at,
            "wall_clock_s": round(time.monotonic() - started, 3),
        },
    }
    manifest_path.write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="krpaly_derive.extract", description=__doc__)
    parser.add_argument("--pbf", required=True, type=Path, help="the pinned OSM extract to read")
    parser.add_argument("--out", required=True, type=Path, help="directory to write the stage into")
    parser.add_argument(
        "--boundary-relation",
        type=int,
        default=DEFAULT_BOUNDARY_RELATION,
        help=f"OSM relation id of the kraj (default: {DEFAULT_BOUNDARY_RELATION})",
    )
    parser.add_argument(
        "--buffer-m",
        type=float,
        default=DEFAULT_BUFFER_M,
        help=f"metres to buffer the boundary by (default: {DEFAULT_BUFFER_M:.0f})",
    )
    parser.add_argument(
        "--index",
        default=DEFAULT_INDEX,
        help=f"libosmium node location index (default: {DEFAULT_INDEX})",
    )
    parser.add_argument(
        "--force", action="store_true", help="re-derive even if the output is already current"
    )
    args = parser.parse_args(argv)

    # A missing file or a boundary that does not assemble is the operator's
    # mistake, not a crash: say what is wrong on one line rather than making
    # them read a traceback for it.
    try:
        return extract(
            pbf=args.pbf,
            out=args.out,
            boundary_relation=args.boundary_relation,
            buffer_m=args.buffer_m,
            index=args.index,
            force=args.force,
        )
    except ExtractError as error:
        raise SystemExit(f"extract: {error}") from error


if __name__ == "__main__":
    raise SystemExit(main())
