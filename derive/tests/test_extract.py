"""The extraction stage, over the committed fixture.

No DATABASE_URL skipif: none of this needs Postgres, which is the point of
testing the stage before there is a loader to test it through.

Every assertion here is one of #6's own done-whens — the junction split, both
directions, populated way refs, and a re-run that produces the identical
file — rather than a restatement of what the code does.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest
import shapely
from pyarrow import parquet as pq

from krpaly_derive.cyclable import (
    PREDICATE_VERSION,
    STRUCTURE_VERSION,
    is_cyclable,
    structure_of,
)
from krpaly_derive.extract import MANIFEST_NAME, OUTPUT_NAME, main

FIXTURE = Path(__file__).parent / "fixtures" / "junctions.osm.pbf"

# The relation id the fixture's boundary carries; not the Moravskoslezský
# default, so a run over the fixture has to be told about it.
FIXTURE_RELATION = 300
FIXTURE_RELATION_VERSION = 42

# The forward segments the fixture must yield, as (way id, node ids). This
# is the ticket's done-when written down: A and B split at the junction they
# share, H splits where it revisits a node, and everything else is in or out
# whole.
EXPECTED_SEGMENTS = {
    (201, (1, 2, 3)),  # A, cut at 3 where B crosses
    (201, (3, 4)),
    (202, (5, 3)),  # B, cut at the same node
    (202, (3, 6)),
    (205, (11, 12)),  # E, a grade1 track, unsplit
    (207, (15, 16)),  # G, access=private reopened by bicycle=designated
    (208, (17, 18)),  # H, cut where the lollipop rejoins itself
    (208, (18, 19, 20, 18)),
    (211, (40, 41)),  # K, a viaduct
    (212, (42, 43)),  # L, a tunnel
    (213, (44, 45)),  # M, covered
    (214, (46, 47)),  # N, bridge=no, which is ground
}

# The structure each way is on; every other way in the fixture is on the
# ground.
STRUCTURES = {211: "bridge", 212: "tunnel", 213: "covered"}


def run(out: Path, *extra: str) -> int:
    return main(
        [
            "--pbf",
            str(FIXTURE),
            "--out",
            str(out),
            "--boundary-relation",
            str(FIXTURE_RELATION),
            *extra,
        ]
    )


@pytest.fixture(scope="module")
def extracted(tmp_path_factory) -> Path:
    """One extraction, read by most of the tests below.

    Module-scoped because the run is the expensive part and none of the tests
    that share it write to the directory.
    """
    out = tmp_path_factory.mktemp("extracted")
    assert run(out) == 0
    return out


def rows(out: Path) -> list[dict]:
    return pq.read_table(out / OUTPUT_NAME).to_pylist()


def manifest(out: Path) -> dict:
    return json.loads((out / MANIFEST_NAME).read_text())


@pytest.mark.parametrize(
    ("tags", "expected"),
    [
        # The in-list, and the one link type that survives its parent being
        # out.
        ({"highway": "residential"}, True),
        ({"highway": "unclassified"}, True),
        ({"highway": "tertiary"}, True),
        ({"highway": "cycleway"}, True),
        ({"highway": "motorway_link"}, True),
        ({"highway": "motorway"}, False),
        # Not a road, and a bicycle grant does not promote it into one.
        ({"highway": "path"}, False),
        ({"highway": "path", "bicycle": "designated"}, False),
        ({"highway": "footway"}, False),
        # Tracks: surfaced in, everything else out, untagged out.
        ({"highway": "track", "tracktype": "grade1"}, True),
        ({"highway": "track", "tracktype": "grade2"}, True),
        ({"highway": "track", "tracktype": "grade3"}, False),
        ({"highway": "track", "tracktype": "grade4"}, False),
        ({"highway": "track"}, False),
        # Access, and the grant that overrides it — in one direction only.
        ({"highway": "residential", "access": "private"}, False),
        ({"highway": "residential", "access": "no"}, False),
        ({"highway": "residential", "access": "private", "bicycle": "designated"}, True),
        ({"highway": "residential", "access": "no", "bicycle": "yes"}, True),
        ({"highway": "residential", "access": "private", "bicycle": "no"}, False),
        ({"highway": "residential", "bicycle": "no"}, False),
        # An access value that is not an exclusion stays in.
        ({"highway": "residential", "access": "destination"}, True),
        # Not a highway at all.
        ({"waterway": "stream"}, False),
        ({}, False),
    ],
)
def test_cyclable_predicate(tags: dict[str, str], expected: bool) -> None:
    assert is_cyclable(tags) is expected


@pytest.mark.parametrize(
    ("tags", "expected"),
    [
        ({"bridge": "viaduct"}, "bridge"),
        ({"bridge": "no"}, None),
        ({"tunnel": "building_passage"}, "tunnel"),
        ({"covered": "yes"}, "covered"),
        # Real combinations, so the precedence is stated rather than left to
        # dict order: a covered bridge is a bridge.
        ({"bridge": "yes", "tunnel": "yes"}, "bridge"),
        ({"bridge": "yes", "covered": "yes"}, "bridge"),
        ({"bridge": "no", "tunnel": "yes"}, "tunnel"),
        ({}, None),
    ],
)
def test_structure_of(tags: dict[str, str], expected: str | None) -> None:
    assert structure_of(tags) == expected


def test_junction_split(extracted: Path) -> None:
    """Exactly the segments the fixture is built to produce, and no others.

    The negative half is carried by the fixture rather than by extra
    assertions: C is a path, D an unsurfaced track, F access=private, I sits
    outside the buffer, and J crosses A at node 2 but is not cyclable, so A
    must not split there.
    """
    forward = {
        (row["way_refs"][0], tuple(row["node_ids"]))
        for row in rows(extracted)
        if row["direction"] == "forward"
    }
    assert forward == EXPECTED_SEGMENTS


def test_junction_degree_ignores_uncyclable_ways(extracted: Path) -> None:
    """Way J shares node 2 with way A, and must not split it.

    Called out separately from the segment set because it is the reason
    degree is counted over cyclable ways rather than over all of them, and a
    regression here would read as one extra segment in a set of eight.
    """
    starts = {tuple(row["node_ids"]) for row in rows(extracted) if row["direction"] == "forward"}
    assert (1, 2) not in starts
    assert (1, 2, 3) in starts


def test_way_outside_buffer_is_dropped(extracted: Path) -> None:
    ways = {row["way_refs"][0] for row in rows(extracted)}
    assert 209 not in ways
    assert manifest(extracted)["counts"]["segments_outside_buffer"] == 1


def test_both_directions(extracted: Path) -> None:
    """Every forward row has exactly one reverse partner, and it is its mirror."""
    table = rows(extracted)
    forward = [row for row in table if row["direction"] == "forward"]
    reverse = [row for row in table if row["direction"] == "reverse"]
    assert len(forward) == len(reverse) == len(EXPECTED_SEGMENTS)
    assert len(table) == 2 * len(forward)

    by_nodes: dict[tuple[int, ...], list[dict]] = {}
    for row in reverse:
        by_nodes.setdefault(tuple(row["node_ids"]), []).append(row)

    for row in forward:
        partners = by_nodes[tuple(row["node_ids"][::-1])]
        assert len(partners) == 1
        partner = partners[0]
        assert partner["way_refs"] == row["way_refs"]
        assert partner["start_node_id"] == row["end_node_id"]
        assert partner["end_node_id"] == row["start_node_id"]
        forward_coords = list(shapely.from_wkb(row["geometry"]).coords)
        assert list(shapely.from_wkb(partner["geometry"]).coords) == forward_coords[::-1]

    counts = manifest(extracted)["counts"]
    assert counts["candidates"] == 2 * counts["segments"]


def test_way_refs_populated(extracted: Path) -> None:
    """The anchor is present on every row, and the geometry agrees with it.

    `way_refs` is a one-element list at this stage because splitting happens
    within a way; the column is a list because a climb spans several.
    """
    for row in rows(extracted):
        assert row["way_refs"], row
        assert len(row["way_refs"]) == 1
        assert row["node_ids"][0] == row["start_node_id"]
        assert row["node_ids"][-1] == row["end_node_id"]
        assert row["n_points"] == len(row["node_ids"])
        assert len(shapely.from_wkb(row["geometry"]).coords) == row["n_points"]


def test_structure_column(extracted: Path) -> None:
    """Each row carries its way's structure, and both directions carry the same one.

    Read off the way rather than the segment because #6 splits only within a
    way, so a candidate is wholly on a structure or wholly off it.
    """
    for row in rows(extracted):
        assert row["structure"] == STRUCTURES.get(row["way_refs"][0]), row


def test_structures_are_counted_not_dropped(extracted: Path) -> None:
    counts = manifest(extracted)["counts"]
    assert counts["segments_bridge"] == 1
    assert counts["segments_tunnel"] == 1
    assert counts["segments_covered"] == 1
    # I, outside the buffer, and nothing else: a structure is not a reason to drop.
    assert counts["segments_dropped"] == 1
    assert manifest(extracted)["way_filter"]["structure"] == STRUCTURE_VERSION


def test_records_the_boundary_version_from_the_extract(extracted: Path) -> None:
    """The version in the file, not the live one — the whole point of reading it here."""
    boundary = manifest(extracted)["boundary"]
    assert boundary["relation_version"] == FIXTURE_RELATION_VERSION
    assert boundary["relation_id"] == FIXTURE_RELATION
    assert boundary["assignment"] == "summit"
    assert manifest(extracted)["way_filter"]["version"] == PREDICATE_VERSION


def test_deterministic_rerun(tmp_path: Path) -> None:
    """Delete the output and re-run, and the bytes are the same bytes.

    This is #6's done-when, asserted rather than described: the Parquet's
    sha256 is equal, and so is the manifest once `run` — the one block
    allowed to differ — is dropped.
    """
    first, second = tmp_path / "a", tmp_path / "b"
    assert run(first) == 0
    assert run(second) == 0

    digests = [
        hashlib.sha256((out / OUTPUT_NAME).read_bytes()).hexdigest() for out in (first, second)
    ]
    assert digests[0] == digests[1]

    manifests = []
    for out, digest in zip((first, second), digests, strict=True):
        content = manifest(out)
        assert content["output"]["sha256"] == digest
        assert content["run"]["started_at"]
        del content["run"]
        manifests.append(content)
    assert manifests[0] == manifests[1]


def test_skips_completed_run(tmp_path: Path, capsys) -> None:
    """A second run leaves the file alone; --force redoes it."""
    out = tmp_path / "out"
    assert run(out) == 0
    output = out / OUTPUT_NAME
    before = output.stat().st_mtime_ns

    assert run(out) == 0
    assert output.stat().st_mtime_ns == before
    assert "already derived" in capsys.readouterr().err

    assert run(out, "--force") == 0
    assert output.stat().st_mtime_ns != before


def test_the_record_is_the_manifest_minus_run(tmp_path: Path) -> None:
    """#21's defect: the committed copy would have carried this machine's path."""
    out, record = tmp_path / "kraj-1", tmp_path / "record"
    assert run(out, "--record", str(record)) == 0

    written = manifest(out)
    text = (record / MANIFEST_NAME).read_text()
    assert str(FIXTURE.resolve()) not in text
    assert written["run"]["pbf_path"] == str(FIXTURE.resolve())
    assert written["osm_snapshot"]["file"] == FIXTURE.name
    assert "path" not in written["osm_snapshot"]
    del written["run"]
    assert json.loads(text) == written


def test_a_skipped_run_restores_a_deleted_record(tmp_path: Path, capsys) -> None:
    out, record = tmp_path / "kraj-1", tmp_path / "record"
    assert run(out, "--record", str(record)) == 0
    committed = (record / MANIFEST_NAME).read_bytes()
    (record / MANIFEST_NAME).unlink()

    assert run(out, "--record", str(record)) == 0
    assert "already derived" in capsys.readouterr().err
    assert (record / MANIFEST_NAME).read_bytes() == committed


def test_a_manifest_in_the_old_shape_asks_for_force(tmp_path: Path) -> None:
    """Written before #21, it carries the absolute path a record must not."""
    out, record = tmp_path / "kraj-1", tmp_path / "record"
    assert run(out, "--record", str(record)) == 0
    old = manifest(out)
    del old["osm_snapshot"]["file"]
    old["osm_snapshot"]["path"] = old["run"].pop("pbf_path")
    (out / MANIFEST_NAME).write_text(json.dumps(old))

    with pytest.raises(SystemExit) as raised:
        run(out, "--record", str(record))
    assert "osm_snapshot.path" in str(raised.value)
    assert "--force" in str(raised.value)
    assert run(out, "--record", str(record), "--force") == 0


def test_rederives_when_the_buffer_changes(tmp_path: Path) -> None:
    """The skip is keyed on everything that changes the output, not just the input file."""
    out = tmp_path / "out"
    assert run(out) == 0
    before = (out / OUTPUT_NAME).stat().st_mtime_ns

    assert run(out, "--buffer-m", "500") == 0
    assert (out / OUTPUT_NAME).stat().st_mtime_ns != before
    assert manifest(out)["boundary"]["buffer_m"] == 500.0


def test_rederives_when_the_manifest_predates_structures(tmp_path: Path) -> None:
    """A run from before #22 has no structure column, and #8 must not read it as all ground."""
    out = tmp_path / "out"
    assert run(out) == 0
    before = (out / OUTPUT_NAME).stat().st_mtime_ns
    old = manifest(out)
    del old["way_filter"]["structure"]
    (out / MANIFEST_NAME).write_text(json.dumps(old))

    assert run(out) == 0
    assert (out / OUTPUT_NAME).stat().st_mtime_ns != before
    assert manifest(out)["way_filter"]["structure"] == STRUCTURE_VERSION


def test_boundary_relation_missing(tmp_path: Path) -> None:
    """A wrong --boundary-relation is one line, not a silently empty extract."""
    with pytest.raises(SystemExit) as raised:
        run(tmp_path / "out", "--boundary-relation", "999999")
    assert "999999" in str(raised.value)
    assert not (tmp_path / "out" / OUTPUT_NAME).exists()


def test_accepts_a_file_backed_index(tmp_path: Path) -> None:
    """`sparse_file_array,<path>` is a location index, not a typo.

    A machine short of RAM passes the file-backed form, and it carries its
    cache path after a comma — so the type name is only the part before it.
    """
    out = tmp_path / "out"
    assert run(out, "--index", f"sparse_file_array,{tmp_path / 'locations.idx'}") == 0
    assert len(rows(out)) == 2 * len(EXPECTED_SEGMENTS)


def test_unknown_index(tmp_path: Path) -> None:
    with pytest.raises(SystemExit) as raised:
        run(tmp_path / "out", "--index", "nonsense")
    assert "nonsense" in str(raised.value)


def test_missing_pbf(tmp_path: Path) -> None:
    with pytest.raises(SystemExit) as raised:
        main(["--pbf", str(tmp_path / "nope.osm.pbf"), "--out", str(tmp_path / "out")])
    assert "nope.osm.pbf" in str(raised.value)


def test_geoparquet_metadata(extracted: Path) -> None:
    """The `geo` key #8 and anything else reading the file will look for."""
    metadata = pq.read_schema(extracted / OUTPUT_NAME).metadata
    geo = json.loads(metadata[b"geo"])
    assert geo["primary_column"] == "geometry"
    assert geo["columns"]["geometry"]["encoding"] == "WKB"
    # Absent, not null. GeoParquet reads an absent `crs` as OGC:CRS84 and an
    # explicit null as "CRS unknown" — the two are different claims, and this
    # file is making the first one.
    assert "crs" not in geo["columns"]["geometry"]
