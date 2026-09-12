"""The runs #9 detects over: ordered chains of candidates that meet at a junction.

A candidate is one stretch of one way between two junctions — 189 m on average
in kraj-1 — and a real climb crosses dozens of them. This is what turns #8's
profiles into chains long enough for `detectClimbs` to see a climb whole, and
it is a module of its own because it is a graph algorithm with its own failure
modes, testable without the engine stage's Node process.

**It covers rather than enumerates.** Elevation increases strictly along a
chain of rising candidates, so that graph is a DAG: the best path through every
candidate is two dynamic-programming passes, and emitting the best path through
each candidate not yet covered puts every rising candidate on a run, in the
longest ascent that contains it. The enumeration this replaces — every maximal
path, capped per seed — was measured on kraj-1, and the cap fired on 49–76 % of
seeds, dropping branches by stack order rather than by terrain: 294 483 runs
over 12.6 M candidate-instances reaching 89.7 % of rising candidates, against
43 532 runs over 393 k reaching all of them, with the same context length
(median 1 485 m against 1 488 m).

**A dip ends a chain**, and `DIP_DROP_M` is off for what it measured rather
than because it is hard. Bridging dips on kraj-1 added 387 345 edges to the
95 066 real ones, and maximising gain over that graph chains the range into one
25 km traverse — every run pinned at `MAX_RUN_M` — while *losing* coverage:
100 % → 91.4 % at a 5 % valley ratio, → 82.7 % at `MERGE_VALLEY_RATIO`'s 20 %.
Crossing a saddle is a real thing to want, but what picks a climb out of a
dipped profile is `detectClimbs`, which already merges valleys inside the
profile it is handed. Doing it here means reimplementing the detector in the
walk, and detection is a climb-engine pull request. The parameters stay as
flags so the experiment is re-runnable without editing this file; the numbers
above are on #10.
"""

from __future__ import annotations

import heapq
from collections import defaultdict
from collections.abc import Mapping
from dataclasses import dataclass

import numpy as np

# In detect's stage signature, so a changed walk re-derives rather than finding
# a stale climbs.parquet "already detected from these inputs".
POLICY_VERSION = 1

# Longer than any Czech climb. It bounds a path that the DAG alone does not:
# a ridge of ever-rising candidates is a legal chain of any length.
MAX_RUN_M = 25_000.0

# Off. See the module docstring — every setting of these measured worse than
# leaving them off, and the engine merges valleys itself.
DIP_DROP_M = 0.0
# MERGE_MAX_GAP_M: how far a bridge may reach for the next rise, when enabled.
DIP_GAP_M = 1200.0
# MERGE_VALLEY_RATIO's shape: a drop is also capped at this share of the gain
# already made, so a 25 m dip needs a climb worth dipping into. 0 disables it
# and leaves DIP_DROP_M as the only bound.
DIP_RATIO = 0.0

# A run is an ordered chain of candidate ids, each ending where the next starts.
Run = tuple[int, ...]


@dataclass(frozen=True, eq=False)
class Profile:
    """One candidate's profile, #8's columns joined to #6's end nodes.

    It lives here rather than in `detect` so that the walk — and its tests —
    do not import the engine stage to name their own input.
    """

    start_node_id: int
    end_node_id: int
    distance_m: np.ndarray
    elevation_m: np.ndarray
    lat: np.ndarray
    lon: np.ndarray


def parameters() -> dict[str, float]:
    """What the walk was run with, for the manifest and the stage signature."""
    return {
        "max_run_m": MAX_RUN_M,
        "dip_drop_m": DIP_DROP_M,
        "dip_gap_m": DIP_GAP_M,
        "dip_ratio": DIP_RATIO,
    }


@dataclass(frozen=True, eq=False)
class _Graph:
    """The rising candidates and how they chain, read once off the profiles."""

    gain: dict[int, float]
    length: dict[int, float]
    top: dict[int, float]
    successors: dict[int, list[int]]
    # Every edge's connector candidates and the drop under the start's top —
    # empty and 0.0 for a chain that meets at a junction.
    via: dict[tuple[int, int], tuple[Run, float]]


def _read(profiles: Mapping[int, Profile]) -> tuple[dict[int, float], ...]:
    """Each candidate's foot, top and length, in one pass over the arrays."""
    foot, top, length = {}, {}, {}
    for candidate_id, profile in profiles.items():
        foot[candidate_id] = float(profile.elevation_m[0])
        top[candidate_id] = float(profile.elevation_m[-1])
        length[candidate_id] = float(profile.distance_m[-1] - profile.distance_m[0])
    return foot, top, length


def _bridges(
    profiles: Mapping[int, Profile],
    rising: set[int],
    top: dict[int, float],
    foot: dict[int, float],
    length: dict[int, float],
    by_start: dict[int, list[int]],
) -> dict[tuple[int, int], tuple[Run, float]]:
    """Edges from a top across a dip to a higher rise, when `DIP_DROP_M` allows.

    A bounded Dijkstra by distance out of each rising candidate's end node,
    over *every* candidate rather than only the rising ones, refusing anything
    that would take the chain more than `DIP_DROP_M` under the top it left.
    Empty — and the search skipped entirely — while dips are off.
    """
    if DIP_DROP_M <= 0.0 or DIP_GAP_M <= 0.0:
        return {}
    found: dict[tuple[int, int], tuple[Run, float]] = {}
    for start in sorted(rising):
        floor = top[start] - DIP_DROP_M
        queue: list[tuple[float, int, Run, float]] = [
            (0.0, profiles[start].end_node_id, (), top[start])
        ]
        nearest = {profiles[start].end_node_id: 0.0}
        while queue:
            walked, node, connector, low = heapq.heappop(queue)
            if walked > nearest.get(node, walked):
                continue
            for step in by_start.get(node, ()):
                if top[step] < floor or foot[step] < floor:
                    continue
                reached = walked + length[step]
                if reached > DIP_GAP_M:
                    continue
                if step in rising and top[step] > top[start]:
                    drop = top[start] - min(low, foot[step])
                    if found.get((start, step), ((), float("inf")))[1] > drop:
                        found[start, step] = (connector, drop)
                    continue
                end = profiles[step].end_node_id
                if reached < nearest.get(end, float("inf")):
                    nearest[end] = reached
                    heapq.heappush(
                        queue, (reached, end, (*connector, step), min(low, top[step], foot[step]))
                    )
    return found


def _graph(profiles: Mapping[int, Profile]) -> _Graph:
    """The rising candidates, and every edge a chain may take between them."""
    foot, top, length = _read(profiles)
    rising = {candidate_id for candidate_id in profiles if top[candidate_id] > foot[candidate_id]}

    by_start: dict[int, list[int]] = defaultdict(list)
    for candidate_id in sorted(profiles):
        by_start[profiles[candidate_id].start_node_id].append(candidate_id)

    via: dict[tuple[int, int], tuple[Run, float]] = {}
    successors: dict[int, list[int]] = {}
    for candidate_id in sorted(rising):
        onward = []
        for step in by_start.get(profiles[candidate_id].end_node_id, ()):
            # The elevations either side of a junction are the same DEM sample,
            # so the test is free; it is here because a chain that did not rise
            # would be a cycle, and the topological order below assumes none.
            if step in rising and top[step] > top[candidate_id]:
                onward.append(step)
                via[candidate_id, step] = ((), 0.0)
        successors[candidate_id] = onward

    for (start, step), bridge in _bridges(profiles, rising, top, foot, length, by_start).items():
        if step not in successors[start]:
            successors[start].append(step)
            via[start, step] = bridge
    for onward in successors.values():
        onward.sort()

    return _Graph(
        gain={c: top[c] - foot[c] for c in rising},
        # Every candidate, not only the rising ones: a bridge's connector steps
        # are on the run and count towards its length.
        length=dict(length),
        top={c: top[c] for c in rising},
        successors=successors,
        via=via,
    )


def _allowed(drop: float, gained: float) -> bool:
    """Whether a bridge's drop is one the chain has earned the right to cross."""
    if drop <= 0.0:
        return True
    return drop <= DIP_DROP_M and (DIP_RATIO <= 0.0 or drop <= DIP_RATIO * gained)


def _passes(graph: _Graph, order: list[int]) -> tuple[dict, dict, dict, dict]:
    """Best gain into and out of every candidate, with the pointer that made it.

    `order` is a topological order, so one forward sweep settles every
    `best_in` and one backward sweep every `best_out`. Ties go to the lowest
    candidate id, which is what makes two walks agree.
    """
    predecessors: dict[int, list[int]] = defaultdict(list)
    for candidate_id, onward in graph.successors.items():
        for step in onward:
            predecessors[step].append(candidate_id)

    best_in: dict[int, float] = {}
    parent: dict[int, int | None] = {}
    for candidate_id in order:
        best, chosen = 0.0, None
        for previous in sorted(predecessors.get(candidate_id, ())):
            gained = best_in[previous]
            if gained > best and _allowed(graph.via[previous, candidate_id][1], gained):
                best, chosen = gained, previous
        best_in[candidate_id] = best + graph.gain[candidate_id]
        parent[candidate_id] = chosen

    best_out: dict[int, float] = {}
    child: dict[int, int | None] = {}
    for candidate_id in reversed(order):
        best, chosen = 0.0, None
        for step in graph.successors[candidate_id]:
            if best_out[step] > best and _allowed(
                graph.via[candidate_id, step][1], best_in[candidate_id]
            ):
                best, chosen = best_out[step], step
        best_out[candidate_id] = best + graph.gain[candidate_id]
        child[candidate_id] = chosen

    return best_in, best_out, parent, child


def _path(graph: _Graph, candidate_id: int, parent: dict, child: dict) -> list[int]:
    """The best chain through one candidate, bridges expanded into their steps."""
    behind: list[int] = []
    at = candidate_id
    while parent.get(at) is not None:
        previous = parent[at]
        behind = [previous, *graph.via[previous, at][0], *behind]
        at = previous
    ahead: list[int] = []
    at = candidate_id
    while child.get(at) is not None:
        step = child[at]
        ahead = [*ahead, *graph.via[at, step][0], step]
        at = step
    return [*behind, candidate_id, *ahead]


def _trim(graph: _Graph, path: list[int], keep: int) -> tuple[Run, bool]:
    """`path` shortened to `MAX_RUN_M`, from whichever end is further from `keep`.

    `keep` survives whatever happens: it is the candidate the run was emitted
    to cover, and a trim that dropped it would leave it uncovered and the cover
    loop choosing it again.
    """
    total = sum(graph.length[c] for c in path)
    trimmed = total > MAX_RUN_M
    while total > MAX_RUN_M and path[0] != keep:
        total -= graph.length[path.pop(0)]
    while total > MAX_RUN_M and path[-1] != keep:
        total -= graph.length[path.pop()]
    return tuple(path), trimmed


def ascending_runs(profiles: Mapping[int, Profile]) -> tuple[list[Run], dict]:
    """Maximal ascending chains of candidates, and what the walk did.

    Every rising candidate comes back on at least one run. A candidate #8
    dropped for nodata has no profile, is not in the graph, and so ends the
    chains that reach it — nothing may be profiled across a hole in the DEM.
    """
    graph = _graph(profiles)
    order = sorted(graph.gain, key=lambda c: (graph.top[c], c))
    best_in, best_out, parent, child = _passes(graph, order)

    covered: set[int] = set()
    emitted: set[Run] = set()
    at_max = 0
    for candidate_id in sorted(order, key=lambda c: (graph.gain[c] - best_in[c] - best_out[c], c)):
        if candidate_id in covered:
            continue
        run, trimmed = _trim(graph, _path(graph, candidate_id, parent, child), candidate_id)
        at_max += trimmed
        covered.update(run)
        covered.add(candidate_id)
        emitted.add(run)

    runs = sorted(emitted)
    lengths = [sum(graph.length[c] for c in run) for run in runs]
    longest = max(range(len(runs)), key=lambda i: (lengths[i], -i)) if runs else None
    counts = {
        "policy_version": POLICY_VERSION,
        "parameters": parameters(),
        "profiled": len(profiles),
        "rising": len(graph.gain),
        "runs": len(runs),
        "candidates_covered": len(covered & set(graph.gain)),
        "coverage_pct": round(100 * len(covered & set(graph.gain)) / max(len(graph.gain), 1), 3),
        "candidate_instances": sum(len(run) for run in runs),
        "longest_run_m": round(lengths[longest], 1) if runs else 0.0,
        "longest_run_candidates": len(runs[longest]) if runs else 0,
        "runs_at_max_run_m": at_max,
    }
    return runs, counts
