"""The verification tool, over inputs written in the earlier stages' own schemas.

No Node, no DATABASE_URL and no network. The selection rules are pure and
tested as such; the queue, the server and the golden set are run once end to
end over a two-candidate derivation, a ride database built here, and a GPX
track, because what matters there is that a verdict posted to the page comes
out of `accept` as the right road fact.
"""

from __future__ import annotations

import json
import random
import sqlite3
import threading
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path

import numpy as np
import pyarrow as pa
import pytest

from krpaly_derive import verify
from krpaly_derive.anchor import OUTPUT_NAME as ANCHORS_NAME
from krpaly_derive.anchor import SCHEMA as ANCHORS_SCHEMA
from krpaly_derive.extract import FORWARD, candidate, write_parquet, write_table
from krpaly_derive.extract import OUTPUT_NAME as CANDIDATES_NAME
from krpaly_derive.sample import OUTPUT_NAME as PROFILES_NAME
from krpaly_derive.sample import SCHEMA as PROFILES_SCHEMA

# ~111 m of latitude per 0.001°, the unit every position below is built in.
DEG_PER_M = 1 / 111_195


def a_climb(climb_id: int, **overrides) -> dict:
    """One row of #10's anchors, in the schema it writes."""
    return {
        "climb_id": climb_id,
        "way_refs": [100 + climb_id],
        "start_node_id": 1,
        "end_node_id": 2,
        "candidate_ids": [0],
        "start_offset_m": 0.0,
        "end_offset_m": 1000.0,
        "dist_m": 1000.0,
        "gain_m": 60.0,
        "avg_grade_pct": 6.0,
        "max_grade_pct": 9.0,
        "start_lat": 49.5,
        "start_lon": 18.5,
        "top_lat": 49.5 + 1000 * DEG_PER_M,
        "top_lon": 18.5,
        "difficulty": 100.0,
        "category": "4",
        "run_id": climb_id,
        "climb_index": 0,
        "collapsed": 0,
    } | overrides


def samples(elevations: list[float], step_m: float = 10.0) -> np.ndarray:
    """`[distance_m, elevation_m, lat, lon]` rows, as load.profile_of gives them."""
    return np.array(
        [[i * step_m, e, 49.5 + i * step_m * DEG_PER_M, 18.5] for i, e in enumerate(elevations)]
    )


# --- pathologies -------------------------------------------------------------


def test_a_climb_with_nothing_wrong_raises_no_flag() -> None:
    assert verify.flags_of(a_climb(0), samples([300, 301, 302, 303]), {}) == {}


def test_each_pathology_is_flagged_with_a_line_saying_why() -> None:
    row = a_climb(
        0,
        max_grade_pct=31.0,
        avg_grade_pct=2.5,
        dist_m=8000.0,
        candidate_ids=[0, 1],
    )
    flags = verify.flags_of(row, samples([300, 301, 306, 307]), {1: "tunnel"})

    assert set(flags) == {"steep", "step", "shallow", "structure"}
    assert "31.0" in flags["steep"]
    assert "50 %" in flags["step"]
    assert flags["structure"] == "crosses a tunnel"


def test_a_step_over_samples_closer_than_the_minimum_is_not_a_step() -> None:
    # 3 m up over 2 m apart is a resampling artefact, not a DEM edge.
    close = np.array([[0, 300, 49.5, 18.5], [2, 303, 49.5, 18.5], [12, 303.5, 49.5, 18.5]])

    assert verify.max_step_grade(close) == pytest.approx(0.05)


def test_two_anchors_with_one_start_top_and_length_are_a_duplicate_pair_stronger_first() -> None:
    weaker = a_climb(0, gain_m=55.0)
    stronger = a_climb(1, way_refs=[100, 900])
    elsewhere = a_climb(2, start_lat=49.5 + 500 * DEG_PER_M)
    longer = a_climb(3, dist_m=1500.0)

    pairs = verify.duplicate_pairs([weaker, stronger, elsewhere, longer])

    assert [(a["climb_id"], b["climb_id"]) for a, b in pairs] == [(1, 0)]


def test_the_stratified_sample_caps_every_category_and_keeps_none_as_its_own() -> None:
    rows = [a_climb(i, category="4") for i in range(40)] + [
        a_climb(40 + i, category=None) for i in range(3)
    ]

    first = verify.stratified_sample(rows, 15, random.Random(7))
    again = verify.stratified_sample(rows, 15, random.Random(7))

    strata = [stratum for stratum, _ in first]
    assert strata.count("4") == 15 and strata.count("null") == 3
    assert [r["climb_id"] for _, r in first] == [r["climb_id"] for _, r in again]


# --- rides -------------------------------------------------------------------


def a_ride(**overrides) -> dict:
    climb = a_climb(0)
    return {
        "activity_id": 7,
        "climb_index": 0,
        "distance_m": 1000.0,
        "elevation_m": 60.0,
        "category": "4",
        "start_lat": climb["start_lat"],
        "start_lon": climb["start_lon"],
        "top_lat": climb["top_lat"],
        "top_lon": climb["top_lon"],
        "name": "Ride",
        "date": "2026-09-01",
    } | overrides


@pytest.mark.parametrize(
    ("ride", "outcome"),
    [
        (a_ride(distance_m=1050.0, elevation_m=55.0), "agrees"),
        (a_ride(elevation_m=120.0), "resolves"),
        # 400 m further down the same road: the top resolves, the start does not.
        (a_ride(start_lat=49.5 - 400 * DEG_PER_M), "top"),
        (a_ride(top_lat=49.6), "none"),
    ],
)
def test_a_ride_resolves_as_the_extension_would(ride: dict, outcome: str) -> None:
    index = verify.TopIndex([a_climb(0)])

    got, best = verify.match_ride(ride, index)

    assert got == outcome
    assert (best is None) == (outcome == "none")


def test_a_ride_segment_runs_from_its_start_to_the_first_pass_of_its_top_after_it() -> None:
    # Out through the top, back down past the start, and up again: the segment
    # is the second climb only if the start is matched first.
    lats = [49.50, 49.51, 49.52, 49.51, 49.50, 49.505, 49.51, 49.52]
    track = np.array([[lat, 18.5, 300 + (lat - 49.5) * 10_000] for lat in lats])

    segment = verify.track_segment(track, (49.505, 18.5), (49.52, 18.5))

    assert segment[:, 2].tolist() == [49.505, 49.51, 49.52]
    assert segment[0, 0] == 0.0 and segment[-1, 0] == pytest.approx(1667.9, abs=1)


def test_an_end_is_covered_only_near_a_profile_sample() -> None:
    cells = {(49500, 18500)}

    assert verify.covered(cells, 49.5009, 18.5009)
    assert verify.covered(cells, 49.5015, 18.5)
    assert not verify.covered(cells, 49.504, 18.5)


# --- verdicts and the golden set ---------------------------------------------


def climb_json(climb_id: int, **overrides) -> dict:
    return verify.climb_json(a_climb(climb_id, **overrides), samples([300, 310]), {})


def test_the_latest_verdict_wins_and_a_torn_last_line_is_ignored(tmp_path: Path) -> None:
    log = tmp_path / "verdicts.jsonl"
    log.write_text(
        '{"id": "a", "verdict": "climb"}\n{"id": "a", "verdict": "not_climb"}\n{"id": "b", "ver'
    )

    assert verify.read_verdicts(log) == {"a": {"id": "a", "verdict": "not_climb"}}


def test_verdicts_become_road_facts_and_nothing_about_the_ride() -> None:
    kept, rejected, first, second, found = (climb_json(i) for i in range(5))
    ride = {"start": [49.5, 18.5], "top": [49.51, 18.5], "dist_m": 1000.4, "gain_m": 60.2}
    decisions = [
        ({"kind": "climb", "climbs": [kept]}, "climb", ""),
        ({"kind": "climb", "climbs": [rejected]}, "bad_profile", ""),
        ({"kind": "climb", "climbs": [climb_json(9)]}, "unsure", ""),
        ({"kind": "pair", "climbs": [first, second]}, "duplicate", ""),
        ({"kind": "ride", "climbs": [found], "ride": ride}, "found", ""),
        ({"kind": "ride", "climbs": [], "ride": ride | {"name": "x"}}, "missing", ""),
    ]

    entries = verify.golden_entries(decisions)

    by_ways = {e["anchor"]["way_refs"][0]: e for e in entries if e["anchor"]}
    assert set(by_ways) == {100, 101, 103, 104}
    assert by_ways[100]["verdict"] == "keep" and by_ways[104]["verdict"] == "keep"
    assert (by_ways[101]["verdict"], by_ways[101]["reason"]) == ("reject", "bad_profile")
    assert (by_ways[103]["verdict"], by_ways[103]["reason"]) == ("reject", "duplicate")
    [missing] = [e for e in entries if e["verdict"] == "missing"]
    assert missing["anchor"] is None and missing["dist_m"] == 1000
    assert "name" not in json.dumps(entries)


def test_a_later_verdict_on_the_same_anchor_replaces_the_earlier_one() -> None:
    climb = climb_json(0)
    decisions = [
        ({"kind": "climb", "climbs": [climb]}, "climb", ""),
        ({"kind": "climb", "climbs": [climb]}, "wrong_road", ""),
    ]

    [entry] = verify.golden_entries(decisions)

    assert (entry["verdict"], entry["reason"]) == ("reject", "wrong_road")


def test_precision_weights_strata_by_size_and_names_the_ones_not_reviewed() -> None:
    queue = {"strata": {"4": 300, "null": 100, "HC": 4}}

    def random_item(stratum: str) -> dict:
        return {"bucket": "random", "stratum": stratum}

    decisions = [(random_item("4"), "climb", "")] * 4 + [
        (random_item("null"), "not_climb", ""),
        (random_item("null"), "climb", ""),
        (random_item("null"), "unsure", ""),
        ({"bucket": "steep", "stratum": None}, "not_climb", ""),
    ]

    got = verify.precision(queue, decisions)

    assert got["estimate"] == pytest.approx(0.75 * 1.0 + 0.25 * 0.5)
    assert got["reviewed"] == 6
    assert got["missing_strata"] == ["HC"]


def test_an_all_real_sample_still_carries_a_margin() -> None:
    queue = {"strata": {"4": 300}}
    decisions = [({"bucket": "random", "stratum": "4"}, "climb", "")] * 15

    got = verify.precision(queue, decisions)

    assert got["estimate"] == 1.0 and got["margin"] > 0.05


def test_a_golden_set_is_held_to_a_derivation_one_outcome_per_entry() -> None:
    same = climb_json(0)
    grown = climb_json(1)
    moved = climb_json(2)
    lost = climb_json(3, start_lat=49.0, top_lat=49.01)
    rows = [
        a_climb(0),
        a_climb(1, gain_m=90.0, start_lat=49.2, top_lat=49.21),
        a_climb(20),  # same ends as climb 2, other ways: the anchor moved
        a_climb(4, start_lat=49.3, top_lat=49.31),
    ]

    def keep(c: dict) -> dict:
        return verify.entry("keep", None, c)

    entries = [
        keep(same),
        keep(grown | {"start": [49.2, 18.5], "top": [49.21, 18.5]}),
        keep(moved),
        keep(lost),
        verify.entry("reject", "duplicate", climb_json(4)),
        verify.entry("reject", "wrong_road", climb_json(5)),
        {"verdict": "missing", "anchor": None, "start": [49.3, 18.5], "top": [49.31, 18.5]},
        {"verdict": "missing", "anchor": None, "start": [48.0, 18.5], "top": [48.01, 18.5]},
    ]

    outcomes = [outcome for outcome, _ in verify.check_entries(entries, rows)]

    assert outcomes == [
        "kept",
        "drifted",
        "moved",
        "lost",
        "still_there",
        "gone",
        "found",
        "still_missing",
    ]


# --- end to end --------------------------------------------------------------


def write_derivation(out: Path) -> None:
    """Two candidates up one road, a bridge on the second, and two anchors over them."""
    out.mkdir(parents=True)
    lat = [49.5 + i * 10 * DEG_PER_M for i in range(101)]
    write_parquet(
        [
            candidate(101, [1, 2], [(18.5, lat[0]), (18.5, lat[50])], FORWARD, None),
            candidate(102, [2, 3], [(18.5, lat[50]), (18.5, lat[100])], FORWARD, "bridge"),
        ],
        out / CANDIDATES_NAME,
    )
    halves = [range(0, 51), range(50, 101)]
    write_table(
        pa.table(
            {
                "candidate_id": pa.array([0, 1], pa.uint64()),
                "n_samples": pa.array([51, 51], pa.int32()),
                "distance_m": [[i * 10.0 for i in half] for half in halves],
                "elevation_m": [[300.0 + i * 0.6 for i in half] for half in halves],
                "lat": [[lat[i] for i in half] for half in halves],
                "lon": [[18.5] * 51 for _ in halves],
            },
            schema=PROFILES_SCHEMA,
        ),
        out / PROFILES_NAME,
    )
    rows = [
        a_climb(0, way_refs=[101, 102], end_node_id=3, candidate_ids=[0, 1], top_lat=lat[100]),
        a_climb(1, way_refs=[101], end_node_id=2, candidate_ids=[0], category=None,
                end_offset_m=500.0, dist_m=500.0, gain_m=30.0, top_lat=lat[50]),
    ]  # fmt: skip
    write_table(pa.Table.from_pylist(rows, schema=ANCHORS_SCHEMA), out / ANCHORS_NAME)


def write_rides(tmp_path: Path) -> tuple[Path, Path]:
    db = tmp_path / "garmin.db"
    with sqlite3.connect(db) as connection:
        connection.executescript(
            """
            create table activities (activity_id integer primary key, name text, date text,
                                     type text);
            create table climbs (activity_id integer, climb_index integer, distance_m real,
                                 elevation_m real, category text, start_lat real,
                                 start_lon real, top_lat real, top_lon real, avg_hr integer);
            """
        )
        connection.execute("insert into activities values (7, 'Ride', '2026-09-01', 'cycling')")
        connection.execute("insert into activities values (8, 'Run', '2026-09-02', 'running')")
        climb = a_climb(0)
        # The second ride tops out 700 m up the road: on it, and 200 m from
        # either climb's top, so nothing resolves it.
        off_top = 49.5 + 700 * DEG_PER_M
        for activity, index, top_lat in (
            (7, 0, climb["top_lat"]),
            (7, 1, off_top),
            (8, 0, off_top),
        ):
            connection.execute(
                "insert into climbs values (?, ?, 1000, 60, '4', ?, ?, ?, 18.5, 150)",
                (activity, index, climb["start_lat"], climb["start_lon"], top_lat),
            )
    gpx = tmp_path / "gpx"
    gpx.mkdir()
    points = "".join(
        f'<trkpt lat="{49.5 + i * 100 * DEG_PER_M}" lon="18.5"><ele>{300 + i * 6}</ele></trkpt>'
        for i in range(11)
    )
    (gpx / "7.gpx").write_text(
        f'<gpx xmlns="http://www.topografix.com/GPX/1/1"><trk><trkseg>{points}</trkseg></trk></gpx>'
    )
    return db, gpx


@pytest.fixture
def served(tmp_path: Path):
    out = tmp_path / "kraj-1"
    write_derivation(out)
    db, gpx = write_rides(tmp_path)
    assert verify.queue_stage(out, db, gpx, seed=1) == 0

    server = ThreadingHTTPServer(("127.0.0.1", 0), verify.handler_for(out))
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield out, f"http://127.0.0.1:{server.server_address[1]}"
    server.shutdown()
    server.server_close()


def post(url: str, body: dict) -> int:
    request = urllib.request.Request(url + "/verdict", data=json.dumps(body).encode())
    try:
        with urllib.request.urlopen(request) as response:
            return response.status
    except urllib.error.HTTPError as error:
        return error.code


def test_the_queue_holds_every_bucket_its_inputs_call_for(served) -> None:
    out, _ = served
    queue = verify.read_queue(out)
    by_bucket = {}
    for item in queue["items"]:
        by_bucket.setdefault(item["bucket"], []).append(item)

    assert set(by_bucket) == {"ride", "random", "structure"}
    # The run is not a ride; the second ride tops out where nothing does.
    rides = {item["id"]: item for item in by_bucket["ride"]}
    assert set(rides) == {"ride:7:0", "ride:7:1"}
    assert rides["ride:7:0"]["auto"] == "found"
    assert len(rides["ride:7:0"]["ride"]["samples"]) == 11
    assert rides["ride:7:1"]["auto"] is None
    assert queue["strata"] == {"4": 1, "null": 1}
    # Only what the page draws: the heart rate in the ride database never enters.
    assert set(rides["ride:7:0"]["ride"]) == {
        "name", "date", "category", "dist_m", "gain_m", "start", "top", "samples"
    }  # fmt: skip


def test_a_verdict_posted_to_the_page_comes_out_of_accept_as_a_road_fact(
    served, tmp_path: Path
) -> None:
    out, url = served
    queue = verify.read_queue(out)
    [random_top] = [
        item for item in queue["items"] if item["bucket"] == "random" and item["stratum"] == "4"
    ]
    with urllib.request.urlopen(url + "/") as page:
        assert b"krpaly review" in page.read()

    assert post(url, {"id": random_top["id"], "verdict": "climb"}) == 200
    assert post(url, {"id": "ride:7:1", "verdict": "missing", "note": "the old road"}) == 200
    assert post(url, {"id": "ride:7:1", "verdict": "sideways"}) == 400
    assert post(url, {"id": "nothing", "verdict": "climb"}) == 400
    with urllib.request.urlopen(url + "/verdicts.json") as response:
        assert json.load(response)["ride:7:1"]["note"] == "the old road"

    verified = tmp_path / "verified.json"
    assert verify.accept_stage(out, verified) == 0
    document = json.loads(verified.read_text())
    assert document["counts"] == {"keep": 1, "missing": 1}
    assert document["climbs"][0]["anchor"]["way_refs"] == [101, 102]
    assert "Ride" not in verified.read_text()

    assert verify.check_stage(out, verified) == 0


def test_check_fails_when_a_kept_climb_is_gone(served, tmp_path: Path) -> None:
    out, url = served
    verified = tmp_path / "verified.json"
    verified.write_text(
        json.dumps({"climbs": [verify.entry("keep", None, climb_json(0, start_lat=48.0))]})
    )

    assert verify.check_stage(out, verified) == 1


def test_gpx_without_rides_is_refused() -> None:
    with pytest.raises(SystemExit, match="needs --rides"):
        verify.main(["queue", "--out", "x", "--gpx", "y"])
