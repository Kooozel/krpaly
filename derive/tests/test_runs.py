"""The walk, over graphs small enough to read.

No Node, no database and no I/O: `ascending_runs` is a pure function over the
mapping `detect.load_profiles` returns, so a test builds its graph out of
`Profile` directly and every assertion here is about the graph rather than
about the engine.
"""

from __future__ import annotations

import numpy as np
import pytest

from krpaly_derive import runs as runs_module
from krpaly_derive.runs import MAX_RUN_M, Profile, Run, ascending_runs


def leg(start_node: int, end_node: int, length_m: float, gain_m: float) -> Profile:
    """One candidate, a straight ramp between two junctions."""
    distance = np.array([0.0, length_m])
    return Profile(
        start_node_id=start_node,
        end_node_id=end_node,
        distance_m=distance,
        elevation_m=np.array([300.0, 300.0 + gain_m]),
        lat=np.array([49.5, 49.5]),
        lon=np.array([18.5, 18.5]),
    )


def chain(*legs: tuple[int, int, float, float]) -> dict[int, Profile]:
    """Candidates numbered in the order given, as #6 numbers them."""
    return {index: leg(*spec) for index, spec in enumerate(legs)}


def at(profiles: dict[int, Profile], foot_m: float) -> dict[int, Profile]:
    """The same candidates, with every elevation lifted by `foot_m`."""
    return {
        candidate_id: Profile(
            start_node_id=profile.start_node_id,
            end_node_id=profile.end_node_id,
            distance_m=profile.distance_m,
            elevation_m=profile.elevation_m + foot_m,
            lat=profile.lat,
            lon=profile.lon,
        )
        for candidate_id, profile in profiles.items()
    }


def stacked(*legs: tuple[int, int, float, float]) -> dict[int, Profile]:
    """A chain whose candidates actually meet in elevation, not only at a node.

    Each leg starts where the one before it ended, which is what the real
    sampler produces: both sides of a junction are the same DEM sample.
    """
    profiles: dict[int, Profile] = {}
    height: dict[int, float] = {}
    for index, (start_node, end_node, length_m, gain_m) in enumerate(legs):
        foot = height.get(start_node, 0.0)
        profiles[index] = at({0: leg(start_node, end_node, length_m, gain_m)}, foot)[0]
        height[end_node] = foot + gain_m
    return profiles


def rising_of(profiles: dict[int, Profile]) -> set[int]:
    return {
        candidate_id
        for candidate_id, profile in profiles.items()
        if profile.elevation_m[-1] > profile.elevation_m[0]
    }


def covers(runs: list[Run], profiles: dict[int, Profile]) -> bool:
    """The invariant the cover exists for: no rising candidate is left out."""
    return rising_of(profiles) <= {c for run in runs for c in run}


def test_a_straight_chain_is_one_run_over_all_of_it() -> None:
    profiles = stacked((1, 2, 400, 20), (2, 3, 400, 20), (3, 4, 400, 20), (4, 5, 400, 20))
    runs, counts = ascending_runs(profiles)

    assert runs == [(0, 1, 2, 3)]
    assert counts["rising"] == 4
    assert counts["candidates_covered"] == 4
    assert counts["coverage_pct"] == 100.0
    assert counts["longest_run_candidates"] == 4
    assert counts["longest_run_m"] == pytest.approx(1600.0)


def test_a_y_below_one_summit_is_two_runs_sharing_their_tail() -> None:
    # Two roads meet at node 3 and climb on together to node 4.
    profiles = stacked((1, 3, 500, 30), (2, 3, 500, 30), (3, 4, 500, 30))
    runs, counts = ascending_runs(profiles)

    assert len(runs) == 2
    assert {run[0] for run in runs} == {0, 1}
    # The shared tail is on both, which is the case the dedupe has to tell
    # apart from a duplicate.
    assert all(run[-1] == 2 for run in runs)
    assert covers(runs, profiles)
    assert counts["coverage_pct"] == 100.0


def test_a_falling_candidate_ends_the_chain_and_is_on_no_run() -> None:
    profiles = stacked((1, 2, 400, 30), (2, 3, 400, -30), (3, 4, 400, 30))
    runs, counts = ascending_runs(profiles)

    assert runs == [(0,), (2,)]
    assert counts["rising"] == 2
    assert covers(runs, profiles)


def test_a_flat_grid_produces_no_runs() -> None:
    profiles = stacked(*[(node, node + 1, 300, 0.0) for node in range(1, 20)])
    runs, counts = ascending_runs(profiles)

    assert runs == []
    assert counts["rising"] == 0
    assert counts["runs"] == 0
    assert counts["coverage_pct"] == 0.0


def test_a_candidate_with_no_profile_breaks_the_chain_there() -> None:
    # #8 dropped candidate 1 for nodata, so nothing may be profiled across it.
    profiles = stacked((1, 2, 400, 20), (2, 3, 400, 20), (3, 4, 400, 20))
    del profiles[1]
    runs, _ = ascending_runs(profiles)

    assert runs == [(0,), (2,)]


def test_the_longest_ascent_wins_the_junction() -> None:
    # Two ways into node 3; the run through the one that climbed more is the
    # one emitted first, and the other is still covered.
    profiles = stacked((1, 3, 400, 10), (2, 3, 400, 80), (3, 4, 400, 40))
    runs, _ = ascending_runs(profiles)

    assert runs[0] == (0, 2)
    assert (1, 2) in runs
    assert covers(runs, profiles)


def test_max_run_m_trims_the_chain_and_is_counted() -> None:
    legs = [(node, node + 1, 6000.0, 40.0) for node in range(1, 7)]
    runs, counts = ascending_runs(stacked(*legs))

    assert counts["runs_at_max_run_m"] > 0
    assert all(sum(6000.0 for _ in run) <= MAX_RUN_M for run in runs)
    assert covers(runs, stacked(*legs))


def test_two_walks_over_the_same_profiles_agree() -> None:
    profiles = stacked((1, 3, 400, 30), (2, 3, 400, 10), (3, 4, 400, 30), (3, 5, 400, 50))
    first, first_counts = ascending_runs(profiles)
    second, second_counts = ascending_runs(profiles)

    assert first == second
    assert first_counts == second_counts


def test_every_rising_candidate_is_covered_on_a_branching_graph() -> None:
    profiles = stacked(
        (1, 2, 300, 20),
        (2, 3, 300, 20),
        (2, 4, 300, 10),
        (3, 5, 300, 30),
        (4, 5, 300, 40),
        (5, 6, 300, 20),
        (7, 3, 300, 15),
    )
    runs, counts = ascending_runs(profiles)

    assert covers(runs, profiles)
    assert counts["coverage_pct"] == 100.0
    assert counts["candidates_covered"] == counts["rising"] == 7


# --- dips, which are off by default ------------------------------------------


def dipped() -> dict[int, Profile]:
    """Up 30 m, down 10 m over the next candidate, up 40 m again."""
    return stacked((1, 2, 400, 30), (2, 3, 400, -10), (3, 4, 400, 40))


def test_a_dip_ends_the_chain_while_dips_are_off() -> None:
    runs, counts = ascending_runs(dipped())

    assert runs == [(0,), (2,)]
    assert counts["parameters"]["dip_drop_m"] == 0.0


def test_a_dip_within_the_drop_is_bridged_when_it_is_turned_on(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The §03 experiment, which measured worse — see the module docstring."""
    monkeypatch.setattr(runs_module, "DIP_DROP_M", 25.0)
    runs, counts = ascending_runs(dipped())

    # The falling candidate is on the run: it is what the chain crossed.
    assert runs == [(0, 1, 2)]
    assert counts["parameters"]["dip_drop_m"] == 25.0
    # It is still not a rising candidate, so it is not what coverage counts.
    assert counts["rising"] == counts["candidates_covered"] == 2


def test_a_dip_deeper_than_the_drop_still_ends_the_chain(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(runs_module, "DIP_DROP_M", 5.0)
    runs, _ = ascending_runs(dipped())

    assert runs == [(0,), (2,)]


def test_a_dip_is_refused_when_too_deep_for_the_gain_so_far(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """MERGE_VALLEY_RATIO's shape: a 10 m dip needs a climb worth dipping into."""
    monkeypatch.setattr(runs_module, "DIP_DROP_M", 25.0)
    monkeypatch.setattr(runs_module, "DIP_RATIO", 0.2)
    # 20 % of the 30 m climbed so far is 6 m, and the dip is 10 m.
    assert ascending_runs(dipped())[0] == [(0,), (2,)]

    monkeypatch.setattr(runs_module, "DIP_RATIO", 0.5)
    assert ascending_runs(dipped())[0] == [(0, 1, 2)]


def test_a_dip_further_than_the_gap_is_not_reached_for(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(runs_module, "DIP_DROP_M", 25.0)
    monkeypatch.setattr(runs_module, "DIP_GAP_M", 100.0)
    # The connector is 400 m long, so the next rise is out of reach.
    assert ascending_runs(dipped())[0] == [(0,), (2,)]
