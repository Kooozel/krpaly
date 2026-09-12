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


@dataclass(frozen=True)
class Parameters:
    """What the walk was run with: the defaults, or what `detect` was told.

    Passed rather than read off the module so the §03 experiment is a flag on
    the stage, and so the manifest records what a run actually used rather
    than what this file happens to say today.
    """

    max_run_m: float = MAX_RUN_M
    dip_drop_m: float = DIP_DROP_M
    dip_gap_m: float = DIP_GAP_M
    dip_ratio: float = DIP_RATIO

    def as_dict(self) -> dict[str, float]:
        """For the manifest's `runs` block and the stage signature."""
        return {
            "max_run_m": self.max_run_m,
            "dip_drop_m": self.dip_drop_m,
            "dip_gap_m": self.dip_gap_m,
            "dip_ratio": self.dip_ratio,
        }

    def allows(self, drop: float, gained: float) -> bool:
        """Whether a bridge's drop is one the chain has earned the right to cross."""
        if drop <= 0.0:
            return True
        return drop <= self.dip_drop_m and (
            self.dip_ratio <= 0.0 or drop <= self.dip_ratio * gained
        )

    @property
    def bridges_dips(self) -> bool:
        return self.dip_drop_m > 0.0 and self.dip_gap_m > 0.0


@dataclass(frozen=True, eq=False)
class Heights:
    """Every candidate's foot, top and length, read once off its profile."""

    foot: dict[int, float]
    top: dict[int, float]
    length: dict[int, float]


@dataclass(frozen=True, eq=False)
class Graph:
    """The rising candidates and how they chain, read once off the profiles."""

    gain: dict[int, float]
    length: dict[int, float]
    top: dict[int, float]
    successors: dict[int, list[int]]
    # Every edge's connector candidates and the drop under the start's top —
    # empty and 0.0 for a chain that meets at a junction.
    via: dict[tuple[int, int], tuple[Run, float]]


def heights_of(profiles: Mapping[int, Profile]) -> Heights:
    """Each candidate's foot, top and length, in one pass over the arrays.

    `length` is `distance_m[-1] − distance_m[0]`, which is the figure
    `detect.join_profiles` accumulates and `anchor` maps a climb back onto:
    the shared junction vertex is never counted twice.
    """
    foot, top, length = {}, {}, {}
    for candidate_id, profile in profiles.items():
        foot[candidate_id] = float(profile.elevation_m[0])
        top[candidate_id] = float(profile.elevation_m[-1])
        length[candidate_id] = float(profile.distance_m[-1] - profile.distance_m[0])
    return Heights(foot=foot, top=top, length=length)


def bridges(
    profiles: Mapping[int, Profile],
    rising: set[int],
    heights: Heights,
    by_start: dict[int, list[int]],
    parameters: Parameters,
) -> dict[tuple[int, int], tuple[Run, float]]:
    """Edges from a top across a dip to a higher rise, when `DIP_DROP_M` allows.

    A bounded Dijkstra by distance out of each rising candidate's end node,
    over *every* candidate rather than only the rising ones, refusing anything
    that would take the chain more than `DIP_DROP_M` under the top it left.
    Empty — and the search skipped entirely — while dips are off.
    """
    if not parameters.bridges_dips:
        return {}
    foot, top, length = heights.foot, heights.top, heights.length
    found: dict[tuple[int, int], tuple[Run, float]] = {}
    for start in sorted(rising):
        floor = top[start] - parameters.dip_drop_m
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
                if reached > parameters.dip_gap_m:
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


def graph_of(profiles: Mapping[int, Profile], parameters: Parameters) -> Graph:
    """The rising candidates, and every edge a chain may take between them."""
    heights = heights_of(profiles)
    foot, top, length = heights.foot, heights.top, heights.length
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

    for (start, step), bridge in bridges(profiles, rising, heights, by_start, parameters).items():
        if step not in successors[start]:
            successors[start].append(step)
            via[start, step] = bridge
    for onward in successors.values():
        onward.sort()

    return Graph(
        gain={c: top[c] - foot[c] for c in rising},
        # Every candidate, not only the rising ones: a bridge's connector steps
        # are on the run and count towards its length.
        length=dict(length),
        top={c: top[c] for c in rising},
        successors=successors,
        via=via,
    )


def passes(
    graph: Graph, order: list[int], parameters: Parameters
) -> tuple[dict[int, float], dict[int, float], dict[int, int | None], dict[int, int | None]]:
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
            if gained > best and parameters.allows(graph.via[previous, candidate_id][1], gained):
                best, chosen = gained, previous
        best_in[candidate_id] = best + graph.gain[candidate_id]
        parent[candidate_id] = chosen

    best_out: dict[int, float] = {}
    child: dict[int, int | None] = {}
    for candidate_id in reversed(order):
        best, chosen = 0.0, None
        for step in graph.successors[candidate_id]:
            if best_out[step] > best and parameters.allows(
                graph.via[candidate_id, step][1], best_in[candidate_id]
            ):
                best, chosen = best_out[step], step
        best_out[candidate_id] = best + graph.gain[candidate_id]
        child[candidate_id] = chosen

    return best_in, best_out, parent, child


def path_through(graph: Graph, candidate_id: int, parent: dict, child: dict) -> list[int]:
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


def trim(graph: Graph, path: list[int], keep: int, max_run_m: float) -> tuple[Run, bool]:
    """`path` shortened to `MAX_RUN_M`, from whichever end is further from `keep`.

    `keep` survives whatever happens: it is the candidate the run was emitted
    to cover, and a trim that dropped it would leave it uncovered and the cover
    loop choosing it again.
    """
    total = sum(graph.length[c] for c in path)
    trimmed = total > max_run_m
    while total > max_run_m and path[0] != keep:
        total -= graph.length[path.pop(0)]
    while total > max_run_m and path[-1] != keep:
        total -= graph.length[path.pop()]
    return tuple(path), trimmed


def ascending_runs(
    profiles: Mapping[int, Profile], parameters: Parameters | None = None
) -> tuple[list[Run], dict]:
    """Maximal ascending chains of candidates, and what the walk did.

    Every rising candidate comes back on at least one run. A candidate #8
    dropped for nodata has no profile, is not in the graph, and so ends the
    chains that reach it — nothing may be profiled across a hole in the DEM.
    """
    parameters = parameters or Parameters()
    graph = graph_of(profiles, parameters)
    order = sorted(graph.gain, key=lambda c: (graph.top[c], c))
    best_in, best_out, parent, child = passes(graph, order, parameters)

    covered: set[int] = set()
    emitted: set[Run] = set()
    at_max = 0
    for candidate_id in sorted(order, key=lambda c: (graph.gain[c] - best_in[c] - best_out[c], c)):
        if candidate_id in covered:
            continue
        run, trimmed = trim(
            graph,
            path_through(graph, candidate_id, parent, child),
            candidate_id,
            parameters.max_run_m,
        )
        at_max += trimmed
        covered.update(run)
        covered.add(candidate_id)
        emitted.add(run)

    runs = sorted(emitted)
    lengths = [sum(graph.length[c] for c in run) for run in runs]
    longest = max(range(len(runs)), key=lambda i: (lengths[i], -i)) if runs else None
    counts = {
        "policy_version": POLICY_VERSION,
        "parameters": parameters.as_dict(),
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
