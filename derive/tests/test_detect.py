"""The engine stage, over synthetic profiles and the vendored build.

No DATABASE_URL and no network. Node is the one requirement: the harness is a
Node process running the committed climb-engine build, and a mocked engine
would test the mock. The skipif mirrors test_migrate.py's REQUIRES_DB; CI puts
Node on PATH, so CI never skips.

Profiles are built here, as in test_sample.py: a flat approach, a ramp at a
known grade, flat again, on 10 m steps. The engine smooths, so the assertions
are the analytic values with a tolerance rather than its digits.
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
from pathlib import Path

import numpy as np
import pyarrow as pa
import pyarrow.parquet as pq
import pytest

from krpaly_derive import detect
from krpaly_derive.detect import (
    MANIFEST_NAME,
    OUTPUT_NAME,
    VERSION_FILE,
    DetectError,
    Harness,
    Profile,
    climb_rows,
    detect_stage,
    join_profiles,
    main,
    read_version,
    single_runs,
)
from krpaly_derive.extract import FORWARD, REVERSE, candidate, sha256_of, write_parquet, write_table
from krpaly_derive.sample import MANIFEST_NAME as PROFILES_MANIFEST
from krpaly_derive.sample import OUTPUT_NAME as PROFILES_NAME
from krpaly_derive.sample import SCHEMA as PROFILES_SCHEMA

REQUIRES_NODE = pytest.mark.skipif(
    shutil.which("node") is None,
    reason="needs Node ≥20 on PATH; the harness is a Node process",
)

ENGINE_COMMIT = "9fb96def4e9f9d9a3487c1c4701246ec1c42579d"


def ramp(
    length_m: float, grade_pct: float, lead_m: float = 1000.0, tail_m: float = 1000.0
) -> list[list[float]]:
    """Flat, `length_m` at `grade_pct`, flat: `[distance, elevation, lat, lon]` every 10 m."""
    d = np.arange(0.0, lead_m + length_m + tail_m + 5.0, 10.0)
    e = 300.0 + np.clip(d - lead_m, 0.0, length_m) * grade_pct / 100
    return np.column_stack([d, e, 49.5 + d / 111_000, np.full_like(d, 18.5)]).tolist()


def reverse(points: list[list[float]]) -> list[list[float]]:
    """The same road ridden the other way, measured from its other end."""
    length = points[-1][0]
    return [[length - d, e, lat, lon] for d, e, lat, lon in reversed(points)]


# --- the vendored build ------------------------------------------------------


def test_the_committed_version_is_the_pinned_build() -> None:
    version = read_version()
    assert version["tag"] == "v0.1.0"
    assert version["source_commit"] == ENGINE_COMMIT
    assert re.fullmatch(r"\d{4}-\d{2}-\d{2}", version["vendored_on"])


@pytest.mark.parametrize(
    ("replacement", "match"),
    [("", "source_commit"), ("source_commit: 9fb96de\n", "40")],
    ids=["missing", "short"],
)
def test_a_version_without_a_full_sha_raises(tmp_path: Path, replacement: str, match: str) -> None:
    # The same rule as derivation_engine_commit_is_sha, failing here rather
    # than at load: a stage that ran for an hour should not be refused by the
    # database afterwards for a fact that was knowable before it started.
    text = re.sub(r"^source_commit:.*\n", replacement, VERSION_FILE.read_text(), flags=re.M)
    path = tmp_path / "VERSION"
    path.write_text(text)
    with pytest.raises(DetectError, match=match):
        read_version(path)


# --- runs --------------------------------------------------------------------


def profile(start: int, end: int, distances: list[float], elevations: list[float]) -> Profile:
    d = np.asarray(distances, dtype=np.float64)
    return Profile(
        start_node_id=start,
        end_node_id=end,
        distance_m=d,
        elevation_m=np.asarray(elevations, dtype=np.float32),
        lat=49.5 + d / 111_000,
        lon=np.full_like(d, 18.5),
    )


def test_single_runs_is_one_run_per_candidate_in_order() -> None:
    assert single_runs([4, 9, 2]) == [(4,), (9,), (2,)]


def test_a_run_of_two_offsets_the_second_and_shares_the_junction() -> None:
    profiles = {
        0: profile(10, 20, [0.0, 10.0, 20.0], [300.0, 301.0, 302.0]),
        1: profile(20, 30, [0.0, 10.0, 20.0, 25.0], [302.0, 303.0, 304.0, 305.0]),
    }
    points = join_profiles((0, 1), profiles)
    # n1 + n2 − 1: the junction vertex is the last sample of one candidate and
    # the first of the next, and a repeated distance is a zero-length segment.
    assert len(points) == 3 + 4 - 1
    assert [p[0] for p in points] == [0.0, 10.0, 20.0, 30.0, 40.0, 45.0]
    assert [p[1] for p in points] == [300.0, 301.0, 302.0, 303.0, 304.0, 305.0]


def test_a_run_whose_candidates_do_not_meet_raises() -> None:
    profiles = {
        0: profile(10, 20, [0.0, 10.0], [300.0, 301.0]),
        1: profile(21, 30, [0.0, 10.0], [301.0, 302.0]),
    }
    with pytest.raises(DetectError, match="20.*21"):
        join_profiles((0, 1), profiles)


def test_a_run_through_a_candidate_with_no_profile_raises() -> None:
    # A candidate #8 dropped for nodata has no profile, and a run through it
    # has a hole where terrain should be.
    profiles = {0: profile(10, 20, [0.0, 10.0], [300.0, 301.0])}
    with pytest.raises(DetectError, match="7"):
        join_profiles((0, 7), profiles)


# --- climb rows --------------------------------------------------------------


A_CLIMB = {
    "distance": 1980.0,
    "elevation": 117.3,
    "avgGrade": 5.93,
    "maxSustainedGradient": 0.06,
    "startDistance": 1000.0,
    "endDistance": 2980.0,
    "markerCoords": {"lat": 49.509, "lon": 18.5},
    "endCoords": {"lat": 49.527, "lon": 18.5},
    "difficulty": 69.5,
    "category": "4",
}


def test_max_grade_enters_krpaly_as_a_percentage() -> None:
    [row] = climb_rows(3, (5, 6), [A_CLIMB])
    assert row["run_id"] == 3
    assert row["candidate_ids"] == [5, 6]
    assert row["avg_grade_pct"] == 5.93
    assert row["max_grade_pct"] == pytest.approx(6.0)


@pytest.mark.parametrize("field", ["markerCoords", "endCoords"])
def test_a_climb_without_coordinates_raises(field: str) -> None:
    # Every input tuple carries a lat and lon, so a null here is a broken
    # profile rather than a climb with no position.
    with pytest.raises(DetectError, match=field):
        climb_rows(0, (0,), [A_CLIMB | {field: None}])


def test_a_category_the_schema_does_not_know_raises() -> None:
    with pytest.raises(DetectError, match="Cat5"):
        climb_rows(0, (0,), [A_CLIMB | {"category": "Cat5"}])


# --- the harness -------------------------------------------------------------


@REQUIRES_NODE
def test_one_process_serves_a_whole_batch(monkeypatch: pytest.MonkeyPatch) -> None:
    # The issue's failure: a process per candidate, over tens of thousands of
    # candidates. Counted rather than timed.
    spawned = []
    real = subprocess.Popen

    def counting(*args, **kwargs):
        spawned.append(args)
        return real(*args, **kwargs)

    monkeypatch.setattr(detect.subprocess, "Popen", counting)
    with Harness({}, "aso") as harness:
        replies = [harness.detect(run_id, ramp(2000, 6)) for run_id in (0, 1, 2)]
    assert len(spawned) == 1
    assert [len(climbs) for climbs in replies] == [1, 1, 1]


@REQUIRES_NODE
def test_a_2km_ramp_at_6_percent_is_one_climb() -> None:
    with Harness({}, "aso") as harness:
        [row] = climb_rows(0, (0,), harness.detect(0, ramp(2000, 6)))
    assert row["gain_m"] == pytest.approx(120, abs=5)
    assert row["dist_m"] == pytest.approx(2000, abs=50)
    assert row["avg_grade_pct"] == pytest.approx(6, abs=0.2)
    # 6, not 0.06: the engine reports this one as a fraction.
    assert row["max_grade_pct"] == pytest.approx(6, abs=0.2)
    assert row["start_distance_m"] == pytest.approx(1000, abs=50)
    assert row["end_distance_m"] == pytest.approx(3000, abs=50)
    assert row["start_lat"] == pytest.approx(49.5 + 1000 / 111_000, abs=5e-4)
    # ASO: 1.98 km × 5.93² ≈ 70, which is category 4.
    assert row["category"] == "4"


@REQUIRES_NODE
def test_the_same_road_the_other_way_is_no_climb_and_still_one_reply() -> None:
    # A climb one way is a descent the other, and zero climbs is still an
    # answer: the next exchange stays in step.
    with Harness({}, "aso") as harness:
        assert harness.detect(0, reverse(ramp(2000, 6))) == []
        assert len(harness.detect(1, ramp(2000, 6))) == 1


@REQUIRES_NODE
def test_a_climb_below_every_threshold_is_kept_uncategorised() -> None:
    # 0.4 km at 4 % scores about 0.38 × 3.86² ≈ 5.7 under ASO, below the
    # `uncategorized` threshold of 8 — and the engine still detects it. Null
    # is data: §01's finding is that most climbs are like this one.
    with Harness({}, "aso") as harness:
        [row] = climb_rows(0, (0,), harness.detect(0, ramp(400, 4)))
    assert row["category"] is None
    assert row["difficulty"] is None


@REQUIRES_NODE
@pytest.mark.parametrize(
    ("override", "key"),
    [
        # Each derived from the other at module load, so overriding the source
        # alone leaves the derived key at the old value, silently.
        ({"RESAMPLE_MIN_INTERVAL_M": 10}, "SPIKE_MAX_SEGMENT_M"),
        ({"CLIMB_LEADIN_GRADE_PCT": 2}, "TRIM_START_GRADE_PCT"),
        # The engine validates nothing: a typo is a no-op.
        ({"NOT_A_KEY": 1}, "NOT_A_KEY"),
        ({"CLIMB_START_GRADE_PCT": float("nan")}, "CLIMB_START_GRADE_PCT"),
        ({"CLIMB_START_GRADE_PCT": "4"}, "CLIMB_START_GRADE_PCT"),
    ],
    ids=["resample-alone", "leadin-alone", "unknown-key", "nan", "string"],
)
def test_an_override_the_engine_would_misread_is_refused(override: dict, key: str) -> None:
    with pytest.raises(DetectError, match=key), Harness(override, "aso"):
        pass


@REQUIRES_NODE
def test_an_unknown_scoring_model_is_refused() -> None:
    with pytest.raises(DetectError, match="strava"), Harness({}, "strava"):
        pass


@REQUIRES_NODE
def test_a_valid_override_is_what_the_engine_runs_with() -> None:
    override = {"RESAMPLE_MIN_INTERVAL_M": 10, "SPIKE_MAX_SEGMENT_M": 20}
    with Harness(override, "aso") as harness:
        assert harness.effective_config["RESAMPLE_MIN_INTERVAL_M"] == 10
        assert harness.effective_config["SPIKE_MAX_SEGMENT_M"] == 20
        # Untouched keys are the build's defaults.
        assert harness.effective_config["CLIMB_START_GRADE_PCT"] == 3.75


def fake_harness(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, source: str) -> None:
    """A stand-in for harness.mjs that misbehaves in one named way."""
    fake = tmp_path / "fake.mjs"
    fake.write_text(source)
    monkeypatch.setattr(detect, "HARNESS", fake)


@REQUIRES_NODE
def test_a_reply_that_is_not_json_is_a_one_line_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # A DetectError, not a JSONDecodeError: main() turns only the first into
    # one line on stderr.
    fake_harness(
        tmp_path, monkeypatch, 'process.stdout.write("not json\\n");\nprocess.stdin.resume();\n'
    )
    with pytest.raises(DetectError, match="not JSON"), Harness({}, "aso"):
        pass


@REQUIRES_NODE
def test_a_reply_about_another_run_is_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Lockstep is what makes an out-of-order reply detectable at all.
    fake_harness(
        tmp_path,
        monkeypatch,
        'import { createInterface } from "node:readline";\n'
        "let first = true;\n"
        "for await (const line of createInterface({ input: process.stdin })) {\n"
        "  process.stdout.write(first\n"
        '    ? \'{"engine": {"effective_config": {}, "node": "fake"}}\\n\'\n'
        '    : \'{"id": 99, "climbs": []}\\n\');\n'
        "  first = false;\n"
        "}\n",
    )
    with pytest.raises(DetectError, match='"id": 99'), Harness({}, "aso") as harness:
        harness.detect(0, ramp(2000, 6))


# --- the stage ---------------------------------------------------------------


def write_stage(out: Path, roads: list[list[list[float]]]) -> None:
    """#6's candidates and #8's profiles, in their own forms: each road both ways.

    Candidate 2k is road k forward, 2k + 1 the same road reversed, as #6 emits
    them. The geometry is two points — this stage never reads it.
    """
    rows, profiles = [], []
    for k, points in enumerate(roads):
        nodes = [10 * k + 1, 10 * k + 2]
        ends = [(points[0][3], points[0][2]), (points[-1][3], points[-1][2])]
        rows.append(candidate(100 + k, nodes, ends, FORWARD))
        rows.append(candidate(100 + k, nodes[::-1], ends[::-1], REVERSE))
        profiles.extend([points, reverse(points)])
    candidates = out / "candidates.parquet"
    write_parquet(rows, candidates)

    columns = [np.asarray(p) for p in profiles]
    table = pa.table(
        {
            "candidate_id": pa.array(range(len(columns)), pa.uint64()),
            "n_samples": pa.array([len(c) for c in columns], pa.int32()),
            "distance_m": [c[:, 0].tolist() for c in columns],
            "elevation_m": [c[:, 1].astype(np.float32).tolist() for c in columns],
            "lat": [c[:, 2].tolist() for c in columns],
            "lon": [c[:, 3].tolist() for c in columns],
        },
        schema=PROFILES_SCHEMA,
    )
    write_table(table, out / PROFILES_NAME)
    # Only the two digests this stage reads out of #8's manifest.
    manifest = {
        "source": {"sha256": sha256_of(candidates)},
        "output": {"sha256": sha256_of(out / PROFILES_NAME)},
    }
    (out / PROFILES_MANIFEST).write_text(json.dumps(manifest))


@REQUIRES_NODE
def test_the_stage_writes_climbs_and_a_derivation_ready_manifest(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    write_stage(tmp_path, [ramp(2000, 6), ramp(400, 4)])
    record = tmp_path / "record"
    assert main(["--out", str(tmp_path), "--record", str(record)]) == 0

    manifest = json.loads((tmp_path / MANIFEST_NAME).read_text())
    committed = json.loads((record / MANIFEST_NAME).read_text())
    assert committed == {block: value for block, value in manifest.items() if block != "run"}
    # Keyed as the derivation columns, so #11 copies this block rather than
    # mapping it.
    assert manifest["derivation"] == {
        "engine_version": "v0.1.0",
        "engine_commit": ENGINE_COMMIT,
        "engine_config_override": {},
        "scoring_model": "aso",
    }
    assert manifest["engine"]["library_sha256"] == sha256_of(detect.LIBRARY)
    counts = manifest["counts"]
    assert (counts["runs"], counts["runs_with_climbs"], counts["climbs"]) == (4, 2, 2)
    assert counts["by_category"]["4"] == 1
    assert counts["by_category"]["null"] == 1

    table = pq.read_table(tmp_path / OUTPUT_NAME)
    # The forward runs climb, the reversed ones descend.
    assert table.column("candidate_ids").to_pylist() == [[0], [2]]
    assert table.column("category").to_pylist() == ["4", None]
    first = sha256_of(tmp_path / OUTPUT_NAME)
    assert manifest["output"]["sha256"] == first

    capsys.readouterr()
    assert main(["--out", str(tmp_path)]) == 0
    assert "already" in capsys.readouterr().err

    (tmp_path / OUTPUT_NAME).unlink()
    assert main(["--out", str(tmp_path)]) == 0
    assert sha256_of(tmp_path / OUTPUT_NAME) == first


@REQUIRES_NODE
def test_a_retune_re_derives_rather_than_reusing(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    write_stage(tmp_path, [ramp(2000, 6)])
    assert main(["--out", str(tmp_path)]) == 0
    capsys.readouterr()

    override = {"CLIMB_START_GRADE_PCT": 4.0}
    detect_stage(
        out=tmp_path,
        profiles=tmp_path / PROFILES_NAME,
        candidates=tmp_path / "candidates.parquet",
        override=override,
        model="aso",
        force=False,
    )
    assert "already" not in capsys.readouterr().err
    manifest = json.loads((tmp_path / MANIFEST_NAME).read_text())
    assert manifest["derivation"]["engine_config_override"] == override


def test_profiles_from_other_candidates_are_refused(tmp_path: Path) -> None:
    # #8's manifest names the candidates it sampled. Joining node ids from a
    # different extraction would chain runs through junctions that are not
    # there.
    write_stage(tmp_path, [ramp(2000, 6)])
    rows = [candidate(999, [1, 2], [(18.5, 49.5), (18.5, 49.6)], FORWARD)]
    write_parquet(rows, tmp_path / "candidates.parquet")
    with pytest.raises(SystemExit, match="candidates"):
        main(["--out", str(tmp_path)])


def test_a_missing_profiles_file_says_which_stage_to_run(tmp_path: Path) -> None:
    with pytest.raises(SystemExit, match="krpaly_derive.sample"):
        main(["--out", str(tmp_path)])
