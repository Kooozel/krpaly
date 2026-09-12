"""The anchor stage, over climbs written in #9's own schema.

No Node, no DATABASE_URL and no network: this stage reads three Parquet files
and writes a fourth, so the inputs are built here with the real schemas rather
than by running the engine. The graphs are small enough to read — what is being
tested is which climbs survive the dedupe and what identity they come out with.
"""

from __future__ import annotations

import json
from pathlib import Path

import pyarrow as pa
import pyarrow.parquet as pq
import pytest

from krpaly_derive.anchor import (
    MANIFEST_NAME,
    OUTPUT_NAME,
    AnchorError,
    anchor_stage,
    main,
)
from krpaly_derive.detect import MANIFEST_NAME as CLIMBS_MANIFEST
from krpaly_derive.detect import OUTPUT_NAME as CLIMBS_NAME
from krpaly_derive.detect import SCHEMA as CLIMBS_SCHEMA
from krpaly_derive.extract import FORWARD, candidate, sha256_of, write_parquet, write_table
from krpaly_derive.extract import OUTPUT_NAME as CANDIDATES_NAME
from krpaly_derive.sample import OUTPUT_NAME as PROFILES_NAME
from krpaly_derive.sample import SCHEMA as PROFILES_SCHEMA


def write_inputs(out: Path, segments: list[tuple], climbs: list[dict]) -> None:
    """#6's candidates, #8's profiles and #9's climbs, in their own forms.

    A segment is `(way_id, start_node, end_node, length_m)` and becomes
    candidate `k` in the order given, as #6 numbers them. Only the two ends of
    a profile are read here, so two samples are a whole profile.
    """
    out.mkdir(parents=True, exist_ok=True)
    rows, lengths = [], []
    for way_id, start_node, end_node, length_m in segments:
        coords = [(18.5, 49.5), (18.5 + length_m / 74_000, 49.5)]
        rows.append(candidate(way_id, [start_node, end_node], coords, FORWARD, None))
        lengths.append(float(length_m))
    candidates = out / CANDIDATES_NAME
    write_parquet(rows, candidates)

    profiles = pa.table(
        {
            "candidate_id": pa.array(range(len(lengths)), pa.uint64()),
            "n_samples": pa.array([2] * len(lengths), pa.int32()),
            "distance_m": [[0.0, length] for length in lengths],
            "elevation_m": [[300.0, 320.0] for _ in lengths],
            "lat": [[49.5, 49.5] for _ in lengths],
            "lon": [[18.5, 18.5] for _ in lengths],
        },
        schema=PROFILES_SCHEMA,
    )
    write_table(profiles, out / PROFILES_NAME)
    write_table(pa.Table.from_pylist(climbs, schema=CLIMBS_SCHEMA), out / CLIMBS_NAME)

    # Only the three digests this stage reads out of #9's manifest.
    manifest = {
        "output": {"sha256": sha256_of(out / CLIMBS_NAME)},
        "source": {
            "profiles_sha256": sha256_of(out / PROFILES_NAME),
            "candidates_sha256": sha256_of(candidates),
        },
    }
    (out / CLIMBS_MANIFEST).write_text(json.dumps(manifest))


def detection(
    run_id: int,
    run: tuple[int, ...],
    start: float,
    end: float,
    gain: float = 100.0,
    index: int = 0,
    category: str | None = "4",
) -> dict:
    """One row of #9's climbs, in the schema it writes."""
    return {
        "run_id": run_id,
        "candidate_ids": list(run),
        "climb_index": index,
        "start_distance_m": start,
        "end_distance_m": end,
        "dist_m": end - start,
        "gain_m": gain,
        "avg_grade_pct": 6.0,
        "max_grade_pct": 9.0,
        "start_lat": 49.5,
        "start_lon": 18.5,
        "top_lat": 49.6,
        "top_lon": 18.6,
        "difficulty": 100.0,
        "category": category,
    }


def run(out: Path, *extra: str) -> int:
    return main(["--out", str(out), "--record", str(out / "record"), *extra])


def rows_of(out: Path) -> list[dict]:
    return pq.read_table(out / OUTPUT_NAME).to_pylist()


def manifest_of(out: Path) -> dict:
    return json.loads((out / MANIFEST_NAME).read_text())


# --- anchoring ---------------------------------------------------------------


def test_a_climb_inside_one_candidate_anchors_to_its_two_nodes(tmp_path: Path) -> None:
    write_inputs(tmp_path, [(101, 1, 2, 1000)], [detection(0, (0,), 100.0, 900.0)])
    assert run(tmp_path) == 0

    (row,) = rows_of(tmp_path)
    assert row["way_refs"] == [101]
    assert (row["start_node_id"], row["end_node_id"]) == (1, 2)
    assert row["candidate_ids"] == [0]
    # Junction nodes, not the detector's exact ends — which are kept as offsets
    # so #11 can clip without re-deriving.
    assert (row["start_offset_m"], row["end_offset_m"]) == (100.0, 900.0)
    assert row["climb_id"] == 0


def test_three_candidates_on_two_ways_give_way_refs_of_length_two(tmp_path: Path) -> None:
    write_inputs(
        tmp_path,
        [(101, 1, 2, 400), (101, 2, 3, 600), (104, 3, 4, 500)],
        [detection(0, (0, 1, 2), 0.0, 1500.0)],
    )
    assert run(tmp_path) == 0

    (row,) = rows_of(tmp_path)
    # How many pieces way 101 was cut into is a fact about node degree, not
    # about the road.
    assert row["way_refs"] == [101, 104]
    assert (row["start_node_id"], row["end_node_id"]) == (1, 4)
    assert manifest_of(tmp_path)["counts"]["multi_candidate"] == 1


def test_a_climb_covers_only_the_candidates_it_reaches(tmp_path: Path) -> None:
    """A climb that opens late and closes on a junction anchors to that range."""
    write_inputs(
        tmp_path,
        [(101, 1, 2, 400), (102, 2, 3, 600), (103, 3, 4, 500)],
        [detection(0, (0, 1, 2), 500.0, 1000.0)],
    )
    assert run(tmp_path) == 0

    (row,) = rows_of(tmp_path)
    # 500 m is inside candidate 1; 1 000 m is exactly the junction at its end,
    # so candidate 2 is not part of the climb.
    assert row["candidate_ids"] == [1]
    assert row["way_refs"] == [102]
    assert (row["start_node_id"], row["end_node_id"]) == (2, 3)
    assert (row["start_offset_m"], row["end_offset_m"]) == (100.0, 600.0)


def test_a_sliver_past_a_junction_does_not_widen_the_anchor(tmp_path: Path) -> None:
    """Both ends are quantized inwards, or a retune of half a metre moves identity.

    The engine reports a position along the profile it was given, and #8's
    samples are 10 m apart, so a climb opening 0,5 m before a junction has not
    climbed the candidate before it.
    """
    write_inputs(
        tmp_path,
        [(101, 1, 2, 400), (102, 2, 3, 600), (103, 3, 4, 500)],
        [detection(0, (0, 1, 2), 399.5, 1000.5)],
    )
    assert run(tmp_path) == 0

    (row,) = rows_of(tmp_path)
    # Without the tolerance on the start, candidate 0 would be dragged in by
    # half a metre and way 101 would be part of the identity.
    assert row["candidate_ids"] == [1]
    assert row["way_refs"] == [102]
    assert (row["start_node_id"], row["end_node_id"]) == (2, 3)


def test_a_collision_is_won_by_gain_not_by_how_finely_it_was_cut(tmp_path: Path) -> None:
    """Two chains on one anchor agree on every way and both nodes.

    How many candidates each was cut into is a fact about node degree, so the
    longer chain is not the better climb; the greater gain is.
    """
    write_inputs(
        tmp_path,
        # Candidate 0 is way 101 whole; candidates 1 and 2 are the same way
        # between the same two junctions, split at an intermediate node.
        [(101, 1, 2, 1000), (101, 1, 5, 400), (101, 5, 2, 600)],
        [
            detection(0, (0,), 0.0, 1000.0, gain=150.0),
            detection(1, (1, 2), 0.0, 1000.0, gain=90.0),
        ],
    )
    assert run(tmp_path) == 0

    (row,) = rows_of(tmp_path)
    assert row["way_refs"] == [101]
    assert row["gain_m"] == 150.0
    assert row["candidate_ids"] == [0]
    assert manifest_of(tmp_path)["counts"]["anchor_collisions"] == 1


# --- the dedupe --------------------------------------------------------------


def test_two_sides_of_one_summit_are_two_climbs(tmp_path: Path) -> None:
    """The regression #10 exists for: summit + gain fused these into one row."""
    write_inputs(
        tmp_path,
        [(101, 1, 9, 1000), (102, 2, 9, 1000)],
        [detection(0, (0,), 0.0, 1000.0, gain=200.0), detection(1, (1,), 0.0, 1000.0, gain=200.0)],
    )
    assert run(tmp_path) == 0

    rows = rows_of(tmp_path)
    assert len(rows) == 2
    assert {tuple(row["way_refs"]) for row in rows} == {(101,), (102,)}
    # One summit, two climbs, and the same gain: the old key had nothing left
    # to tell them apart.
    assert {row["end_node_id"] for row in rows} == {9}
    assert manifest_of(tmp_path)["counts"]["summits_multi"] == 1


def test_the_same_climb_on_two_overlapping_runs_collapses(tmp_path: Path) -> None:
    write_inputs(
        tmp_path,
        [(101, 1, 2, 500), (102, 2, 3, 500), (103, 3, 4, 500)],
        [
            detection(0, (0, 1, 2), 0.0, 1500.0, gain=150.0),
            # The same road, opened later because the run started higher.
            detection(1, (1, 2), 0.0, 1000.0, gain=100.0),
        ],
    )
    assert run(tmp_path) == 0

    (row,) = rows_of(tmp_path)
    assert row["candidate_ids"] == [0, 1, 2]
    assert row["collapsed"] == 1
    counts = manifest_of(tmp_path)["counts"]
    assert (counts["detections"], counts["climbs"], counts["collapsed"]) == (2, 1, 1)


def test_two_approach_variants_sharing_a_tail_both_survive(tmp_path: Path) -> None:
    write_inputs(
        tmp_path,
        [(101, 1, 3, 500), (102, 2, 3, 500), (103, 3, 4, 500)],
        [
            detection(0, (0, 2), 0.0, 1000.0, gain=140.0),
            detection(1, (1, 2), 0.0, 1000.0, gain=130.0),
        ],
    )
    assert run(tmp_path) == 0

    rows = rows_of(tmp_path)
    assert len(rows) == 2
    # Neither chain contains the other, so neither wins: they fork below the
    # summit and share its node.
    assert {tuple(row["way_refs"]) for row in rows} == {(101, 103), (102, 103)}
    assert {row["end_node_id"] for row in rows} == {4}


def test_identical_chains_keep_the_greater_gain(tmp_path: Path) -> None:
    write_inputs(
        tmp_path,
        [(101, 1, 2, 1000)],
        [
            detection(0, (0,), 0.0, 1000.0, gain=80.0),
            detection(1, (0,), 0.0, 1000.0, gain=120.0),
        ],
    )
    assert run(tmp_path) == 0

    (row,) = rows_of(tmp_path)
    assert row["gain_m"] == 120.0
    assert (row["run_id"], row["collapsed"]) == (1, 1)


def test_two_chains_on_one_anchor_leave_one_row_and_are_counted(tmp_path: Path) -> None:
    """The invariant #11's climb_anchor_unique will be held to.

    Two candidate chains that are not suffixes of each other can still collapse
    onto the same way sequence between the same junctions; the load would fail
    on the constraint rather than here.
    """
    write_inputs(
        tmp_path,
        [(101, 1, 2, 1000), (101, 1, 2, 1000)],
        [
            detection(0, (0,), 0.0, 1000.0, gain=90.0),
            detection(1, (1,), 0.0, 1000.0, gain=110.0),
        ],
    )
    assert run(tmp_path) == 0

    (row,) = rows_of(tmp_path)
    assert row["gain_m"] == 110.0
    counts = manifest_of(tmp_path)["counts"]
    assert counts["anchor_collisions"] == 1
    assert counts["climbs"] == 1


def test_climb_ids_are_positions_in_emission_order(tmp_path: Path) -> None:
    write_inputs(
        tmp_path,
        [(101, 1, 9, 500), (102, 2, 9, 500), (103, 3, 9, 500)],
        [
            detection(2, (2,), 0.0, 500.0),
            detection(0, (0,), 0.0, 500.0),
            detection(1, (1,), 0.0, 500.0),
        ],
    )
    assert run(tmp_path) == 0

    rows = rows_of(tmp_path)
    assert [row["climb_id"] for row in rows] == [0, 1, 2]
    assert [row["run_id"] for row in rows] == [0, 1, 2]


# --- the stage ---------------------------------------------------------------


def test_the_stage_writes_anchors_and_a_record_without_a_machine_path(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    out = tmp_path / "kraj-1"
    write_inputs(
        out,
        [(101, 1, 2, 400), (102, 2, 3, 600)],
        [detection(0, (0, 1), 0.0, 1000.0, gain=150.0, category="3")],
    )
    record = out / "record"
    assert run(out) == 0

    manifest = manifest_of(out)
    committed = json.loads((record / MANIFEST_NAME).read_text())
    assert committed == {block: value for block, value in manifest.items() if block != "run"}
    assert str(tmp_path) not in (record / MANIFEST_NAME).read_text()
    assert manifest["output"]["sha256"] == sha256_of(out / OUTPUT_NAME)
    assert manifest["counts"]["by_category"] == {"3": 1}
    assert manifest["counts"]["by_candidates"]["2"] == 1
    assert manifest["counts"]["max_way_refs"] == 2

    first = sha256_of(out / OUTPUT_NAME)
    capsys.readouterr()
    assert run(out) == 0
    assert "already" in capsys.readouterr().err

    # A no-op re-run still restores a deleted record, and a deleted output is
    # rebuilt byte for byte.
    (record / MANIFEST_NAME).unlink()
    assert run(out) == 0
    assert (record / MANIFEST_NAME).is_file()
    (out / OUTPUT_NAME).unlink()
    assert run(out) == 0
    assert sha256_of(out / OUTPUT_NAME) == first


def test_climbs_that_are_not_the_ones_the_manifest_records_are_refused(tmp_path: Path) -> None:
    write_inputs(tmp_path, [(101, 1, 2, 1000)], [detection(0, (0,), 0.0, 1000.0)])
    write_table(
        pa.Table.from_pylist([detection(0, (0,), 0.0, 500.0)], schema=CLIMBS_SCHEMA),
        tmp_path / CLIMBS_NAME,
    )
    with pytest.raises(AnchorError, match="re-run krpaly_derive.detect"):
        anchor_stage(
            out=tmp_path,
            climbs=tmp_path / CLIMBS_NAME,
            profiles=tmp_path / PROFILES_NAME,
            candidates=tmp_path / CANDIDATES_NAME,
            force=False,
            record=tmp_path / "record",
        )


def test_a_missing_input_says_which_stage_to_run(tmp_path: Path) -> None:
    with pytest.raises(SystemExit, match="krpaly_derive.detect"):
        main(["--out", str(tmp_path)])


def test_compare_counts_kept_new_and_lost_anchors(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    baseline = tmp_path / "baseline"
    write_inputs(
        baseline,
        [(101, 1, 2, 1000), (102, 2, 3, 1000)],
        [detection(0, (0,), 0.0, 1000.0), detection(1, (1,), 0.0, 1000.0)],
    )
    assert run(baseline) == 0

    retuned = tmp_path / "retuned"
    write_inputs(
        retuned,
        [(101, 1, 2, 1000), (103, 3, 4, 1000)],
        [detection(0, (0,), 0.0, 1000.0), detection(1, (1,), 0.0, 1000.0)],
    )
    capsys.readouterr()
    assert run(retuned, "--compare", str(baseline / OUTPUT_NAME)) == 0

    # Way 101 survives, way 103 is new, way 102 is lost.
    assert "1 kept, 1 new, 1 lost" in capsys.readouterr().err
    assert "compare" not in manifest_of(retuned)


def test_compare_wants_a_file(tmp_path: Path) -> None:
    write_inputs(tmp_path, [(101, 1, 2, 1000)], [detection(0, (0,), 0.0, 1000.0)])
    with pytest.raises(SystemExit, match="--compare"):
        run(tmp_path, "--compare", str(tmp_path / "nothing.parquet"))
