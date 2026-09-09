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

from krpaly_derive.cyclable import PREDICATE_VERSION, is_cyclable
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
}


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


def test_rederives_when_the_buffer_changes(tmp_path: Path) -> None:
    """The skip is keyed on everything that changes the output, not just the input file."""
    out = tmp_path / "out"
    assert run(out) == 0
    before = (out / OUTPUT_NAME).stat().st_mtime_ns

    assert run(out, "--buffer-m", "500") == 0
    assert (out / OUTPUT_NAME).stat().st_mtime_ns != before
    assert manifest(out)["boundary"]["buffer_m"] == 500.0


def test_boundary_relation_missing(tmp_path: Path) -> None:
    """A wrong --boundary-relation is one line, not a silently empty extract."""
    with pytest.raises(SystemExit) as raised:
        run(tmp_path / "out", "--boundary-relation", "999999")
    assert "999999" in str(raised.value)
    assert not (tmp_path / "out" / OUTPUT_NAME).exists()


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
    # null is GeoParquet's own spelling of OGC:CRS84, and keeps the bytes
    # independent of the installed PROJ.
    assert geo["columns"]["geometry"]["crs"] is None
