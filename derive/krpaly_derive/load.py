"""Stage 6: anchored climbs in, a derivation in Postgres out.

    uv run --directory derive python -m krpaly_derive.load --out data/kraj-1-v2 \
        --pbf ~/data/czech-republic-260901.osm.pbf \
        --osm-snapshot-url https://download.geofabrik.de/europe/czech-republic-260901.osm.pbf

Everything before this stage writes intermediates to disk; this one writes the
database. #10's anchors go into `climb` and `climb_profile` under one
`derivation` row carrying every pinned input.

* **Atomic.** One transaction around every write. A derivation lands entirely
  or not at all, because a half-loaded kraj looks finished.
* **Re-runnable, so `--force` means something else here.** Every other stage's
  `--force` redoes an output that is already current. There is no such thing
  for a load: a second load is *supposed* to be a second derivation, which is
  how a retune stays traceable and reversible. What `--force` permits is the one
  case worth stopping — a derivation whose whole provenance already matches
  the one about to be written, which is an accidental repeat. A retune changes
  the engine commit, its configuration or a filter version, so it is never
  stopped.
* **Assigned by summit**, against the fourteen kraje read out of the same
  snapshot the candidates were extracted from, per derive/INPUTS.md § Border
  policy. A summit outside all of them is outside Czechia, and is counted
  rather than loaded.
* **Nothing is converted.** Both grades are per cent by the time they reach
  this stage — detect converted `maxSustainedGradient` where engine output
  enters krpaly.

`--osm-snapshot-url` is the one value in the row an operator types. No stage
observed it — extract reads a local `.pbf` — and the digest check against
candidates.manifest.json is what proves it names the right bytes.

It names no climbs (`slug` and `name` stay null) and builds no index.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from collections import Counter
from collections.abc import Iterator, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

import numpy as np
import osmium
import pyarrow.parquet as pq
import shapely
from psycopg import sql
from psycopg.types.json import Jsonb
from shapely.prepared import PreparedGeometry, prep

from krpaly_derive.anchor import MANIFEST_NAME as ANCHORS_MANIFEST
from krpaly_derive.anchor import OUTPUT_NAME as ANCHORS_NAME
from krpaly_derive.dem import MANIFEST_NAME as DEM_MANIFEST
from krpaly_derive.detect import MANIFEST_NAME as CLIMBS_MANIFEST
from krpaly_derive.detect import DetectError, join_profiles, load_profiles
from krpaly_derive.extract import DEFAULT_INDEX, sha256_of
from krpaly_derive.extract import MANIFEST_NAME as CANDIDATES_MANIFEST
from krpaly_derive.extract import OUTPUT_NAME as CANDIDATES_NAME
from krpaly_derive.migrate import connect
from krpaly_derive.record import RecordError, record_dir, write_atomically, write_record
from krpaly_derive.runs import Profile
from krpaly_derive.sample import OUTPUT_NAME as PROFILES_NAME

MANIFEST_NAME = "load.manifest.json"

# How far outside the detection window a sample may sit and still be in it.
# The offsets came out of the engine's resampling of these same distances, so
# an endpoint that is exactly a sample must not be lost to a float ulp.
CLIP_TOLERANCE_M = 1e-6


class LoadError(Exception):
    """An input this stage cannot load, said in one line."""


@dataclass(frozen=True)
class Inputs:
    """What the earlier stages recorded, once every digest in the chain agrees."""

    # The `source` block of this stage's manifest: the three files and their digests.
    source: dict
    climbs: dict
    candidates: dict
    dem: dict
    dem_sha256: str


def read_json(path: Path, stage: str) -> dict:
    """A manifest an earlier stage wrote, or a message naming that stage."""
    try:
        manifest = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError) as error:
        raise LoadError(f"{path} cannot be read — run krpaly_derive.{stage}") from error
    if not isinstance(manifest, dict):
        raise LoadError(f"{path} is not a manifest — run krpaly_derive.{stage}")
    return manifest


def read_inputs(anchors: Path, profiles: Path, candidates: Path, pbf: Path, record: Path) -> Inputs:
    """Every input, refused unless the manifests chain from the anchors back to --pbf.

    As `anchor.read_climbs_manifest` chains them, for the same reason one step
    further on: anchoring a climb against another extraction maps its distances
    onto candidates that were never under it, and loading one does the same to
    the database. The `--pbf` link is the one this stage adds, and it is
    load-bearing — the kraj polygons are read from that file, and a summit
    assigned against a boundary from another snapshot is a wrong region_code
    with nothing in the row to say so.

    The DEM half is the **committed** record, never the working copy, because
    `dem_manifest_sha256` points at a file in git.
    """
    anchors_path = anchors.parent / ANCHORS_MANIFEST
    anchors_manifest = read_json(anchors_path, "anchor")
    digests = {
        "anchors_sha256": sha256_of(anchors),
        "profiles_sha256": sha256_of(profiles),
        "candidates_sha256": sha256_of(candidates),
    }
    if anchors_manifest.get("output", {}).get("sha256") != digests["anchors_sha256"]:
        raise LoadError(
            f"{anchors} is not the file {anchors_path} records — re-run krpaly_derive.anchor"
        )
    source = anchors_manifest.get("source", {})
    for name, path_of in (("profiles", profiles), ("candidates", candidates)):
        if source.get(f"{name}_sha256") != digests[f"{name}_sha256"]:
            raise LoadError(
                f"{anchors} was anchored over other {name} than {path_of} — "
                "re-run krpaly_derive.anchor"
            )

    climbs_path = anchors.parent / CLIMBS_MANIFEST
    climbs_manifest = read_json(climbs_path, "detect")
    if climbs_manifest.get("output", {}).get("sha256") != source.get("climbs_sha256"):
        raise LoadError(
            f"{climbs_path} describes other climbs than {anchors} was anchored from — "
            "re-run krpaly_derive.anchor"
        )

    candidates_path = candidates.parent / CANDIDATES_MANIFEST
    candidates_manifest = read_json(candidates_path, "extract")
    if candidates_manifest.get("output", {}).get("sha256") != digests["candidates_sha256"]:
        raise LoadError(
            f"{candidates} is not the file {candidates_path} records — re-run krpaly_derive.extract"
        )
    if candidates_manifest.get("osm_snapshot", {}).get("sha256") != sha256_of(pbf):
        raise LoadError(
            f"{pbf} is not the snapshot {candidates} was extracted from — the kraj boundaries "
            "and the candidates must come from one snapshot"
        )

    dem_path = record / DEM_MANIFEST
    if not dem_path.is_file():
        raise LoadError(f"{dem_path} is not a file — run krpaly_derive.dem, which records it")
    dem_manifest = read_json(dem_path, "dem")
    if dem_manifest.get("source", {}).get("sha256") != digests["candidates_sha256"]:
        raise LoadError(
            f"{dem_path} was fetched for other candidates than {candidates} — "
            "re-run krpaly_derive.dem"
        )

    return Inputs(
        source={
            "anchors": anchors.name,
            "profiles": profiles.name,
            "candidates": candidates.name,
            **digests,
        },
        climbs=climbs_manifest,
        candidates=candidates_manifest,
        dem=dem_manifest,
        dem_sha256=sha256_of(dem_path),
    )


@dataclass(frozen=True)
class Region:
    """A kraj as the pinned extract has it, unbuffered, ready to assign against."""

    code: str
    bounds: tuple[float, float, float, float]
    prepared: PreparedGeometry

    def covers(self, lat: float, lon: float) -> bool:
        """Whether a summit is in this kraj, its border included.

        Bbox reject first, as `extract.Boundary.intersects` does: four float
        compares throw out thirteen kraje before the expensive test. Covers
        rather than contains, which is false on the border itself — a summit
        there would otherwise be in no kraj at all.
        """
        west, south, east, north = self.bounds
        if not (west <= lon <= east and south <= lat <= north):
            return False
        return self.prepared.covers(shapely.Point(lon, lat))


def region_of(code: str, geometry: shapely.Geometry) -> Region:
    return Region(code=code, bounds=geometry.bounds, prepared=prep(geometry))


def read_regions(pbf: Path, codes: Mapping[int, str], index: str) -> list[Region]:
    """Each kraj as the pinned extract has it, in order of code.

    `codes` is `{osm_relation_id: region.code}`, read out of the `region` table
    rather than restated here: 0002 seeded the fourteen relations, the climb's
    `region_code` is a foreign key onto that row, and a second list in Python
    is a second list to be wrong.

    `extract.read_boundary`'s pass, for fourteen relations rather than one and
    with **no buffer** — the buffer exists so that nothing is truncated during
    extraction, and assignment is a point-in-polygon against the real border.
    """
    wkb_factory = osmium.geom.WKBFactory()
    geometries: dict[int, shapely.Geometry] = {}
    seen: set[int] = set()

    processor = (
        osmium.FileProcessor(str(pbf))
        .with_locations(index)
        .with_areas(osmium.filter.IdFilter(list(codes)).enable_for(osmium.osm.RELATION))
        .with_filter(osmium.filter.EntityFilter(osmium.osm.RELATION | osmium.osm.AREA))
    )
    for obj in processor:
        if isinstance(obj, osmium.osm.Area):
            if not obj.from_way() and obj.orig_id() in codes:
                geometries[obj.orig_id()] = shapely.from_wkb(
                    bytes.fromhex(wkb_factory.create_multipolygon(obj))
                )
        elif obj.id in codes:
            seen.add(obj.id)

    # Fatal, and named, as read_boundary makes them: a kraj missing from the
    # list would send its climbs out of Czechia with nothing to say so.
    by_code = sorted(codes.items(), key=lambda item: item[1])
    for relation_id, code in by_code:
        if relation_id not in seen:
            raise LoadError(f"relation {relation_id} ({code}) is not in {pbf} — check --pbf")
        geometry = geometries.get(relation_id)
        if geometry is None or geometry.is_empty:
            raise LoadError(
                f"relation {relation_id} ({code}) is in {pbf} but does not assemble into an area"
            )
    return [region_of(code, geometries[relation_id]) for relation_id, code in by_code]


def assign_region(regions: Sequence[Region], lat: float, lon: float) -> str | None:
    """The kraj a climb belongs to, by its summit, per derive/INPUTS.md § Border policy.

    Tried in order of code, so a summit exactly on a shared border lands in
    the same kraj however the regions were read. None is a summit outside
    Czechia, which the buffer makes real and expected.
    """
    for region in sorted(regions, key=lambda region: region.code):
        if region.covers(lat, lon):
            return region.code
    return None


def profile_of(
    candidate_ids: list[int], profiles: Mapping[int, Profile], start_m: float, end_m: float
) -> list[list[float]] | None:
    """A climb's `[distance_m, elevation_m, lat, lon]` samples, clipped to it.

    Joined by #9's own rule rather than re-derived, so the offsets #10 wrote
    land on the ruler they were measured on. None when fewer than two samples
    fall inside: that is not a linestring, and the caller counts it.
    """
    samples = [
        sample
        for sample in join_profiles(candidate_ids, profiles)
        if start_m - CLIP_TOLERANCE_M <= sample[0] <= end_m + CLIP_TOLERANCE_M
    ]
    return samples if len(samples) >= 2 else None


def derivation_row(inputs: Inputs, snapshot_url: str) -> dict:
    """Every `derivation` column, out of the manifests that already hold them.

    The engine half is `climbs.manifest.json`'s `derivation` block, which #9
    keys as the columns so that it is copied rather than mapped. Two values
    change type on the way in: the replication sequence is a string in the
    `.pbf` header, and the buffer a float on the command line.
    """
    try:
        snapshot = inputs.candidates["osm_snapshot"]
        boundary = inputs.candidates["boundary"]
        way_filter = inputs.candidates["way_filter"]
        dem = inputs.dem["dem"]
        row = {
            **inputs.climbs["derivation"],
            "way_filter_version": way_filter["version"],
            "structure_version": way_filter["structure"],
            "osm_snapshot_url": snapshot_url,
            "osm_snapshot_sha256": snapshot["sha256"],
            "osm_snapshot_replication_ts": snapshot["replication_ts"],
            "osm_snapshot_seq": int(snapshot["replication_seq"]),
            "boundary_relation_id": boundary["relation_id"],
            "boundary_relation_version": boundary["relation_version"],
            "boundary_buffer_m": int(boundary["buffer_m"]),
            "boundary_assignment": boundary["assignment"],
            "dem_product": dem["product"],
            "dem_route": dem["route"],
            "dem_resolution_m": dem["resolution_m"],
            "dem_crs": dem["crs"],
            "dem_vertical_crs": dem["vertical_crs"],
            "dem_nodata_value": dem["nodata_value"],
            "dem_manifest_sha256": inputs.dem_sha256,
            "dem_fetched_at": dem["fetched_at"],
        }
    except (KeyError, TypeError, ValueError) as error:
        raise LoadError(
            f"a manifest lacks or misstates {error} — re-run the stage that wrote it"
        ) from error
    if row["boundary_buffer_m"] != boundary["buffer_m"]:
        raise LoadError(f"boundary.buffer_m {boundary['buffer_m']} is not a whole number of metres")
    return row


CLIMB_COLUMNS = (
    "region_code",
    "way_refs",
    "start_node_id",
    "end_node_id",
    "start_pt",
    "top_pt",
    "dist_m",
    "gain_m",
    "avg_grade",
    "max_grade",
    "difficulty",
    "category",
)


@dataclass(frozen=True)
class Climb:
    """One anchored climb, ready to write: its columns, and its profile's."""

    # CLIMB_COLUMNS, in that order.
    columns: tuple
    anchor: tuple
    region_code: str
    # [elevation_m, lat, lon] per sample, float64 as join_profiles gives them.
    samples: np.ndarray


def point(lat: float, lon: float) -> str:
    """EWKT, longitude first. repr, so a coordinate round-trips exactly."""
    return f"SRID=4326;POINT({lon!r} {lat!r})"


def linestring(samples: np.ndarray) -> str:
    return (
        "SRID=4326;LINESTRING("
        + ", ".join(f"{lon!r} {lat!r}" for _, lat, lon in samples.tolist())
        + ")"
    )


def climbs_of(
    anchors: Path, profiles: Mapping[int, Profile], regions: Sequence[Region]
) -> tuple[list[Climb], Counter]:
    """Every anchored climb that belongs in the database, and why the rest do not.

    A summit outside every kraj is outside Czechia — the 10 km buffer reaches
    into Poland and Slovakia — and `region_code` is a foreign key onto the
    fourteen Czech ones. A window too short to hold two samples cannot be a
    linestring. Both are dropped and counted rather than failing the load.
    """
    dropped: Counter = Counter()
    kept = []
    for row in pq.read_table(anchors).to_pylist():
        region_code = assign_region(regions, row["top_lat"], row["top_lon"])
        if region_code is None:
            dropped["outside_czechia"] += 1
            continue
        try:
            samples = profile_of(
                row["candidate_ids"], profiles, row["start_offset_m"], row["end_offset_m"]
            )
        except DetectError as error:
            raise LoadError(f"{error} — re-run krpaly_derive.anchor") from error
        if samples is None:
            dropped["degenerate_profiles"] += 1
            continue
        anchor = (tuple(row["way_refs"]), row["start_node_id"], row["end_node_id"])
        kept.append(
            Climb(
                columns=(
                    region_code,
                    row["way_refs"],
                    row["start_node_id"],
                    row["end_node_id"],
                    point(row["start_lat"], row["start_lon"]),
                    point(row["top_lat"], row["top_lon"]),
                    row["dist_m"],
                    row["gain_m"],
                    # Both already per cent: detect converted maxSustainedGradient.
                    row["avg_grade_pct"],
                    row["max_grade_pct"],
                    row["difficulty"],
                    row["category"],
                ),
                anchor=anchor,
                region_code=region_code,
                samples=np.array(samples)[:, 1:],
            )
        )
    return kept, dropped


def derivation_params(row: dict) -> Iterator[object]:
    for column, value in row.items():
        yield Jsonb(value) if column == "engine_config_override" else value


def region_codes(cur) -> dict[int, str]:
    cur.execute("select osm_relation_id, code from region")
    return dict(cur.fetchall())


def matching_derivation(cur, row: dict) -> int | None:
    """A derivation whose whole provenance is `row`'s — a repeat, not a retune."""
    cur.execute(
        sql.SQL("select id from derivation where {} order by id limit 1").format(
            sql.SQL(" and ").join(
                sql.SQL("{} = {}").format(sql.Identifier(column), sql.Placeholder())
                for column in row
            )
        ),
        list(derivation_params(row)),
    )
    found = cur.fetchone()
    return None if found is None else found[0]


def insert_derivation(cur, row: dict) -> int:
    cur.execute(
        sql.SQL("insert into derivation ({}) values ({}) returning id").format(
            sql.SQL(", ").join(map(sql.Identifier, row)),
            sql.SQL(", ").join(sql.Placeholder() * len(row)),
        ),
        list(derivation_params(row)),
    )
    return cur.fetchone()[0]


def copy_climbs(cur, derivation_id: int, climbs: Sequence[Climb]) -> dict[tuple, int]:
    """COPY the climbs in, and their ids back by anchor.

    By anchor rather than by insertion order: `climb_anchor_unique` makes the
    key unique within a derivation, so the read-back is exact whatever order
    the rows are returned in.
    """
    statement = sql.SQL("copy climb ({}) from stdin").format(
        sql.SQL(", ").join(map(sql.Identifier, ("derivation_id", *CLIMB_COLUMNS)))
    )
    with cur.copy(statement) as copy:
        for climb in climbs:
            copy.write_row((derivation_id, *climb.columns))
    cur.execute(
        "select id, way_refs, start_node_id, end_node_id from climb where derivation_id = %s",
        (derivation_id,),
    )
    return {(tuple(way_refs), start, end): id_ for id_, way_refs, start, end in cur.fetchall()}


def copy_profiles(cur, ids: Mapping[tuple, int], climbs: Sequence[Climb]) -> None:
    with cur.copy("copy climb_profile (climb_id, geom, elevations) from stdin") as copy:
        for climb in climbs:
            copy.write_row(
                (ids[climb.anchor], linestring(climb.samples), climb.samples[:, 0].tolist())
            )


def report(written: dict) -> None:
    """What landed, on stderr, as the other stages report."""
    counts = written["counts"]
    print(
        f"derivation {written['run']['derivation_id']}: {counts['loaded']} climbs loaded "
        f"of {counts['climbs']} ({counts['outside_czechia']} outside Czechia, "
        f"{counts['degenerate_profiles']} too short to draw)",
        file=sys.stderr,
    )
    split = ", ".join(f"{code} {n}" for code, n in counts["by_region"].items())
    print(f"regions: {split}", file=sys.stderr)
    print(f"load: {written['run']['wall_clock_s']} s", file=sys.stderr)


def load_stage(
    out: Path,
    anchors: Path,
    profiles: Path,
    candidates: Path,
    pbf: Path,
    snapshot_url: str,
    index: str,
    force: bool,
    record: Path | None = None,
) -> int:
    for path, stage in ((anchors, "anchor"), (profiles, "sample"), (candidates, "extract")):
        if not path.is_file():
            raise LoadError(f"{path} is not a file — run krpaly_derive.{stage} first")
    if not pbf.is_file():
        raise LoadError(f"{pbf} is not a file — pass the snapshot the candidates came from")
    record = record or record_dir(out)

    started = time.monotonic()
    started_at = datetime.now(UTC).isoformat(timespec="seconds")

    inputs = read_inputs(anchors, profiles, candidates, pbf, record)
    row = derivation_row(inputs, snapshot_url)

    with connect() as conn:
        with conn.cursor() as cur:
            codes = region_codes(cur)
            existing = None if force else matching_derivation(cur, row)
        # Checked before the slow reads rather than inside the write
        # transaction, so a repeat is refused in seconds. Two loads started
        # at once could both pass it; one operator runs one load, and the
        # cost of that race is a second derivation, which is never a conflict.
        conn.commit()
        if existing is not None:
            raise LoadError(
                f"derivation {existing} already records exactly these inputs — a retune "
                "changes one of them; pass --force to load a second copy anyway"
            )

        # Read before the transaction opens: a kraj's worth of profiles and a
        # pass over the national extract take minutes, and nothing about them
        # needs a lock.
        try:
            by_candidate = load_profiles(profiles, candidates)
        except DetectError as error:
            raise LoadError(f"{error} — re-run krpaly_derive.sample") from error
        regions = read_regions(pbf, codes, index)
        climbs, dropped = climbs_of(anchors, by_candidate, regions)

        # One transaction around every write: a derivation lands entirely or
        # not at all, because a half-loaded kraj looks finished.
        with conn.transaction(), conn.cursor() as cur:
            derivation_id = insert_derivation(cur, row)
            ids = copy_climbs(cur, derivation_id, climbs)
            copy_profiles(cur, ids, climbs)

    manifest = {
        "source": inputs.source,
        # Every value actually written, so the committed record says what the
        # row holds without a database to ask.
        "derivation": row,
        "counts": {
            "climbs": len(climbs) + sum(dropped.values()),
            "loaded": len(climbs),
            "outside_czechia": dropped["outside_czechia"],
            "degenerate_profiles": dropped["degenerate_profiles"],
            "by_region": dict(sorted(Counter(climb.region_code for climb in climbs).items())),
            "profile_points": sum(len(climb.samples) for climb in climbs),
        },
        # The only block allowed to differ between two runs over the same
        # input — and the derivation id is exactly such a fact, so it lives
        # here and the record stays byte-identical across a re-load.
        "run": {
            "started_at": started_at,
            "wall_clock_s": round(time.monotonic() - started, 3),
            "derivation_id": derivation_id,
            "pbf_path": str(pbf.resolve()),
        },
    }
    out.mkdir(parents=True, exist_ok=True)
    write_atomically(
        out / MANIFEST_NAME,
        (json.dumps(manifest, indent=2, sort_keys=True) + "\n").encode("utf-8"),
    )
    write_record(manifest, record, MANIFEST_NAME)
    report(manifest)
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="krpaly_derive.load", description=__doc__)
    parser.add_argument("--out", required=True, type=Path, help="the stage directory #10 wrote")
    parser.add_argument(
        "--anchors",
        type=Path,
        default=None,
        help=f"climbs to load, beside #10's manifest (default: <out>/{ANCHORS_NAME})",
    )
    parser.add_argument(
        "--profiles",
        type=Path,
        default=None,
        help=f"the profiles their geometry is rebuilt from (default: <out>/{PROFILES_NAME})",
    )
    parser.add_argument(
        "--candidates",
        type=Path,
        default=None,
        help=f"the candidates they were anchored over (default: <out>/{CANDIDATES_NAME})",
    )
    parser.add_argument(
        "--pbf",
        required=True,
        type=Path,
        help="the snapshot the candidates were extracted from, which the kraje are read out of",
    )
    parser.add_argument(
        "--osm-snapshot-url",
        required=True,
        help="where that snapshot is published — derive/INPUTS.md § OSM snapshot records it",
    )
    parser.add_argument(
        "--index",
        default=DEFAULT_INDEX,
        help=f"libosmium node location index (default: {DEFAULT_INDEX})",
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="load even if a derivation with exactly this provenance is already in the database",
    )
    parser.add_argument(
        "--record",
        type=Path,
        default=None,
        help="where the committed copy goes, and where #7's is read from "
        "(default: derive/manifests/<name of --out>)",
    )
    args = parser.parse_args(argv)

    try:
        return load_stage(
            out=args.out,
            anchors=args.anchors or args.out / ANCHORS_NAME,
            profiles=args.profiles or args.out / PROFILES_NAME,
            candidates=args.candidates or args.out / CANDIDATES_NAME,
            pbf=args.pbf,
            snapshot_url=args.osm_snapshot_url,
            index=args.index,
            force=args.force,
            record=args.record,
        )
    except (LoadError, RecordError) as error:
        raise SystemExit(f"load: {error}") from error


if __name__ == "__main__":
    raise SystemExit(main())
