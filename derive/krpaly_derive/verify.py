"""Verification: a review queue out of a derivation, verdicts in, a golden set out.

    uv run --directory derive python -m krpaly_derive.verify queue --out data/kraj-1-v2 \
        --rides ~/sport/garmin.db --gpx ~/sport/gpx
    uv run --directory derive python -m krpaly_derive.verify serve --out data/kraj-1-v2
    uv run --directory derive python -m krpaly_derive.verify accept --out data/kraj-1-v2
    uv run --directory derive python -m krpaly_derive.verify check --out data/kraj-1-v3 \
        --verified verified/kraj-1-v2.json

Twenty-four thousand climbs cannot be looked at, and do not need to be. What a
person's time is spent on is decided here, in three kinds of item:

* **Rides.** Climbs the same engine detected on real GPS tracks, matched by the
  resolution API's own question — top within 150 m, start within 250 m. The
  ones that match and agree on distance and gain are decided without anyone
  looking; the rest are the recall misses, and the rider knows those roads.
* **A stratified random sample**, per category. It is the only bucket an
  error rate can be read off, because every other bucket was chosen *for*
  looking wrong. Weighted by stratum size when it is reported.
* **Pathologies** — a max grade no road has, a single step no DEM should give,
  a long shallow merge, a bridge or tunnel, two anchors that are one climb.
  Capped and sampled: these are for learning which stage is wrong, not for
  counting.

Verdicts append to `<out>/review/verdicts.jsonl`, the latest per item winning.
`accept` turns them into `derive/verified/<name of --out>.json`: anchors kept,
anchors rejected with the reason, and climbs a ride proved missing. That file
is committed, and `check` holds a later derivation to it. It carries nothing
about the rides themselves — no activity, no date, no heart rate — only road
facts and a verdict.

The queue and the verdicts stay under `--out`, gitignored, because the ride
half of the queue is personal.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import random
import sqlite3
import sys
import xml.etree.ElementTree as ET
from collections import Counter, defaultdict
from collections.abc import Iterable, Mapping, Sequence
from datetime import UTC, datetime
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import numpy as np
import pyarrow.parquet as pq

from krpaly_derive.anchor import OUTPUT_NAME as ANCHORS_NAME
from krpaly_derive.detect import load_profiles
from krpaly_derive.extract import OUTPUT_NAME as CANDIDATES_NAME
from krpaly_derive.extract import sha256_of
from krpaly_derive.load import profile_of
from krpaly_derive.record import write_atomically
from krpaly_derive.runs import Profile
from krpaly_derive.sample import OUTPUT_NAME as PROFILES_NAME

REVIEW_DIR = "review"
QUEUE_NAME = "queue.json"
VERDICTS_NAME = "verdicts.jsonl"
PAGE = Path(__file__).with_name("review.html")
VERIFIED_ROOT = Path(__file__).resolve().parents[1] / "verified"

# The resolution API's two radii (db/migrations/0004_climb.up.sql): a ride
# matches a climb exactly when the extension would resolve it to that climb.
TOP_M = 150.0
START_M = 250.0

# A ride match close enough to decide without a person: both figures within
# this fraction. GPS and barometer against DMR 5G agree to a few per cent on
# the rows that match at all; a fifth is a different climb, not noise.
RIDE_AGREEMENT = 0.2
# How far around a ride's top the page draws database climbs for comparison.
RIDE_NEARBY_M = 400.0
RIDE_NEARBY_MAX = 5
# A ride end is on derived roads when a profile sample lies in its cell or a
# neighbour: 0.001° is ~110 m of latitude and ~72 m of longitude here.
COVERAGE_CELL_DEG = 0.001

# Pathologies. A sustained 25 % is past every paved road in Czechia; a 40 %
# step between samples at least 5 m apart is a DEM edge, a wall or a deck; a
# 5 km climb averaging under 3 % is usually two climbs merged over a plateau.
STEEP_MAX_GRADE_PCT = 25.0
STEP_GRADE = 0.4
STEP_MIN_M = 5.0
SHALLOW_AVG_GRADE_PCT = 3.0
SHALLOW_MIN_DIST_M = 5000.0
# Two anchors that a rider would call one climb.
DUPLICATE_TOP_M = 50.0
DUPLICATE_START_M = 100.0
DUPLICATE_DIST_RATIO = 0.1

RANDOM_PER_STRATUM = 15
FLAG_CAP = 30
# Samples per drawn line: enough for a profile to read, small enough that a
# few hundred items stay one quick JSON.
MAX_POINTS = 400

# Order is the page's order: what the reviewer knows best, then what can be
# counted, then what is for learning.
BUCKETS = ("ride", "random", "steep", "step", "shallow", "structure", "duplicate")

CLIMB_CHOICES = (
    ("climb", "A real road climb, as drawn"),
    ("not_climb", "No climb here"),
    ("wrong_road", "Not a road anyone rides"),
    ("bad_profile", "Profile is broken — DEM, bridge, tunnel"),
    ("bounds", "A climb, but it starts or ends in the wrong place"),
    ("duplicate", "Another row is the same climb"),
    ("unsure", "Not sure — skip"),
)
PAIR_CHOICES = (
    ("duplicate", "One climb — the second (dashed) is redundant"),
    ("distinct", "Two different climbs"),
    ("unsure", "Not sure — skip"),
)
RIDE_CHOICES = (
    ("found", "The database has it (one of the blue lines)"),
    ("bounds", "The database has it, but starts or ends wrong"),
    ("missing", "The database misses a road climb"),
    ("offroad", "The ride was off-road — not a road climb"),
    ("artefact", "A GPS or barometer artefact, not a climb"),
    ("unsure", "Not sure — skip"),
)
# Which climb verdicts reject the row they were given on.
REJECTING = frozenset({"not_climb", "wrong_road", "bad_profile", "bounds", "duplicate"})


class VerifyError(Exception):
    """An input this tool cannot use, said in one line."""


def metres(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Great-circle distance, haversine."""
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp, dl = p2 - p1, math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * 6_371_000 * math.asin(math.sqrt(a))


def anchor_of(row: Mapping) -> tuple:
    return (tuple(row["way_refs"]), row["start_node_id"], row["end_node_id"])


def anchor_id(anchor: tuple) -> str:
    """A short, stable name for an anchor, so an item id survives a re-queue."""
    way_refs, start, end = anchor
    digest = hashlib.sha1(",".join(map(str, way_refs)).encode()).hexdigest()[:10]
    return f"{start}-{end}-{digest}"


class TopIndex:
    """Climbs bucketed by the cell their top is in, for radius lookups."""

    # Cells per degree. 0.01° is ~720 m of longitude at Czech latitudes, so one
    # ring of neighbours covers every radius asked of it here.
    CELL = 100

    def __init__(self, rows: Iterable[Mapping]):
        self.cells: dict[tuple[int, int], list[Mapping]] = defaultdict(list)
        for row in rows:
            self.cells[self.cell(row["top_lat"], row["top_lon"])].append(row)

    def cell(self, lat: float, lon: float) -> tuple[int, int]:
        return math.floor(lat * self.CELL), math.floor(lon * self.CELL)

    def near_top(self, lat: float, lon: float, radius_m: float) -> list[Mapping]:
        ci, cj = self.cell(lat, lon)
        return [
            row
            for i in (-1, 0, 1)
            for j in (-1, 0, 1)
            for row in self.cells.get((ci + i, cj + j), ())
            if metres(lat, lon, row["top_lat"], row["top_lon"]) <= radius_m
        ]

    def resolve(self, start: Sequence[float], top: Sequence[float]) -> list[Mapping]:
        """What the resolution API would answer: top within TOP_M, start within START_M."""
        return [
            row
            for row in self.near_top(*top, TOP_M)
            if metres(*start, row["start_lat"], row["start_lon"]) <= START_M
        ]


def within(a: float, b: float, ratio: float) -> bool:
    return abs(a - b) <= ratio * max(abs(b), 1e-9)


# --- pathologies -------------------------------------------------------------


def max_step_grade(samples: np.ndarray) -> float:
    """The steepest single step between samples at least STEP_MIN_M apart, as a fraction."""
    dx = np.diff(samples[:, 0])
    dz = np.diff(samples[:, 1])
    wide = dx >= STEP_MIN_M
    return float(np.abs(dz[wide] / dx[wide]).max()) if wide.any() else 0.0


def flags_of(row: Mapping, samples: np.ndarray, structures: Mapping[int, str]) -> dict[str, str]:
    """Every single-row pathology this climb shows, each with a line saying why."""
    flags = {}
    if row["max_grade_pct"] > STEEP_MAX_GRADE_PCT:
        flags["steep"] = f"max sustained grade {row['max_grade_pct']:.1f} %"
    step = max_step_grade(samples)
    if step > STEP_GRADE:
        flags["step"] = f"one step at {step * 100:.0f} %"
    if row["avg_grade_pct"] < SHALLOW_AVG_GRADE_PCT and row["dist_m"] > SHALLOW_MIN_DIST_M:
        flags["shallow"] = (
            f"{row['dist_m'] / 1000:.1f} km at an average {row['avg_grade_pct']:.1f} %"
        )
    kinds = sorted({structures[c] for c in row["candidate_ids"] if c in structures})
    if kinds:
        flags["structure"] = "crosses a " + " and a ".join(kinds)
    return flags


def duplicate_pairs(rows: Sequence[Mapping]) -> list[tuple[Mapping, Mapping]]:
    """Pairs of distinct anchors a rider would call one climb, stronger first."""
    index = TopIndex(rows)
    pairs = []
    for row in rows:
        for other in index.near_top(row["top_lat"], row["top_lon"], DUPLICATE_TOP_M):
            if other["climb_id"] <= row["climb_id"]:
                continue
            start_gap = metres(
                row["start_lat"], row["start_lon"], other["start_lat"], other["start_lon"]
            )
            if start_gap <= DUPLICATE_START_M and within(
                other["dist_m"], row["dist_m"], DUPLICATE_DIST_RATIO
            ):
                first, second = sorted((row, other), key=lambda r: (-r["gain_m"], r["climb_id"]))
                pairs.append((first, second))
    return pairs


def stratified_sample(rows: Sequence[Mapping], per_stratum: int, rng: random.Random) -> list:
    """Up to `per_stratum` rows of every category, `None` its own stratum."""
    strata: dict[str, list[Mapping]] = defaultdict(list)
    for row in rows:
        strata[row["category"] or "null"].append(row)
    picked = []
    for stratum in sorted(strata):
        members = sorted(strata[stratum], key=lambda r: r["climb_id"])
        picked.extend((stratum, row) for row in rng.sample(members, min(per_stratum, len(members))))
    return picked


def capped(items: Sequence, cap: int, rng: random.Random) -> list:
    return list(items) if len(items) <= cap else rng.sample(list(items), cap)


# --- rides -------------------------------------------------------------------


def read_rides(db: Path) -> list[dict]:
    """Cycling climbs from a `~/sport` garmin.db, with the ends they were detected at."""
    if not db.is_file():
        raise VerifyError(f"{db} is not a file — pass ~/sport/garmin.db to --rides")
    with sqlite3.connect(f"file:{db}?mode=ro", uri=True) as connection:
        connection.row_factory = sqlite3.Row
        rows = connection.execute(
            """
            select c.activity_id, c.climb_index, c.distance_m, c.elevation_m, c.category,
                   c.start_lat, c.start_lon, c.top_lat, c.top_lon, a.name, a.date
            from climbs c join activities a using (activity_id)
            where a.type = 'cycling' and c.start_lat is not null and c.top_lat is not null
            order by c.activity_id, c.climb_index
            """
        ).fetchall()
    return [dict(row) for row in rows]


def read_track(path: Path) -> np.ndarray:
    """A GPX track as `[lat, lon, ele]` rows."""
    points = []
    for _, element in ET.iterparse(path):
        if element.tag.endswith("}trkpt"):
            ele = next((c.text for c in element if c.tag.endswith("}ele")), None)
            points.append(
                (float(element.get("lat")), float(element.get("lon")), float(ele or "nan"))
            )
            element.clear()
    return np.array(points, dtype=np.float64).reshape(-1, 3)


def track_segment(track: np.ndarray, start: Sequence[float], top: Sequence[float]) -> np.ndarray:
    """The part of a track between the point nearest `start` and the nearest `top` after it.

    Nearest by position rather than by the ride's own `start_km`: a device's
    odometer and a haversine sum drift apart by hundreds of metres over a long
    ride, and the ends are what was stored exactly.
    """
    if len(track) < 2:
        return np.empty((0, 4))
    scale = math.cos(math.radians(start[0]))

    def nearest(points: np.ndarray, lat: float, lon: float) -> int:
        return int(np.argmin((points[:, 0] - lat) ** 2 + ((points[:, 1] - lon) * scale) ** 2))

    i = nearest(track, *start)
    j = i + nearest(track[i:], *top)
    segment = track[i : j + 1]
    steps = [0.0] + [
        metres(a[0], a[1], b[0], b[1]) for a, b in zip(segment[:-1], segment[1:], strict=True)
    ]
    return np.column_stack([np.cumsum(steps), segment[:, 2], segment[:, 0], segment[:, 1]])


def coverage_cells(profiles: Mapping[int, Profile]) -> set[tuple[int, int]]:
    cells = set()
    for profile in profiles.values():
        cells.update(
            zip(
                np.floor(profile.lat / COVERAGE_CELL_DEG).astype(int).tolist(),
                np.floor(profile.lon / COVERAGE_CELL_DEG).astype(int).tolist(),
                strict=True,
            )
        )
    return cells


def covered(cells: set[tuple[int, int]], lat: float, lon: float) -> bool:
    ci, cj = math.floor(lat / COVERAGE_CELL_DEG), math.floor(lon / COVERAGE_CELL_DEG)
    return any((ci + i, cj + j) in cells for i in (-1, 0, 1) for j in (-1, 0, 1))


def match_ride(ride: Mapping, index: TopIndex) -> tuple[str, Mapping | None]:
    """How a ridden climb resolves: `agrees`, `resolves`, `top`, or `none`, and to what.

    `agrees` is `resolves` with distance and gain inside RIDE_AGREEMENT, and is
    the only outcome decided without a person.
    """
    start, top = (ride["start_lat"], ride["start_lon"]), (ride["top_lat"], ride["top_lon"])
    resolved = index.resolve(start, top)
    if resolved:
        best = min(resolved, key=lambda r: abs(r["dist_m"] - ride["distance_m"]))
        agrees = within(best["dist_m"], ride["distance_m"], RIDE_AGREEMENT) and within(
            best["gain_m"], ride["elevation_m"], RIDE_AGREEMENT
        )
        return ("agrees" if agrees else "resolves"), best
    tops = index.near_top(*top, TOP_M)
    if tops:
        return "top", min(tops, key=lambda r: metres(*start, r["start_lat"], r["start_lon"]))
    return "none", None


def nearby(ride: Mapping, index: TopIndex, best: Mapping | None) -> list[Mapping]:
    """The database climbs worth drawing beside a ride, the best match first."""
    start = (ride["start_lat"], ride["start_lon"])
    around = sorted(
        index.near_top(ride["top_lat"], ride["top_lon"], RIDE_NEARBY_M),
        key=lambda r: metres(*start, r["start_lat"], r["start_lon"]),
    )
    if best is not None:
        around = [best] + [r for r in around if r["climb_id"] != best["climb_id"]]
    return around[:RIDE_NEARBY_MAX]


# --- the queue ---------------------------------------------------------------


def thinned(samples: np.ndarray) -> list[list[float]]:
    """At most MAX_POINTS rows, both ends kept, rounded to what a page can show."""
    if len(samples) > MAX_POINTS:
        keep = np.unique(np.linspace(0, len(samples) - 1, MAX_POINTS).round().astype(int))
        samples = samples[keep]
    return [
        [round(d, 1), round(e, 1), round(lat, 6), round(lon, 6)]
        for d, e, lat, lon in samples.tolist()
    ]


def climb_json(row: Mapping, samples: np.ndarray, structures: Mapping[int, str]) -> dict:
    return {
        "climb_id": row["climb_id"],
        "anchor": {
            "way_refs": list(row["way_refs"]),
            "start_node_id": row["start_node_id"],
            "end_node_id": row["end_node_id"],
        },
        "category": row["category"],
        "dist_m": round(row["dist_m"], 1),
        "gain_m": round(row["gain_m"], 1),
        "avg_grade_pct": round(row["avg_grade_pct"], 2),
        "max_grade_pct": round(row["max_grade_pct"], 2),
        "start": [row["start_lat"], row["start_lon"]],
        "top": [row["top_lat"], row["top_lon"]],
        "structures": sorted({structures[c] for c in row["candidate_ids"] if c in structures}),
        "samples": thinned(samples),
    }


def climb_item(bucket: str, why: str, rows: Sequence[Mapping], samples_of, structures) -> dict:
    kind = "pair" if len(rows) == 2 else "climb"
    return {
        "id": f"{bucket}:" + "+".join(anchor_id(anchor_of(r)) for r in rows),
        "bucket": bucket,
        "kind": kind,
        "why": why,
        "choices": [list(c) for c in (PAIR_CHOICES if kind == "pair" else CLIMB_CHOICES)],
        "auto": None,
        "climbs": [climb_json(r, samples_of(r), structures) for r in rows],
    }


def build_queue(
    anchors: Path,
    profiles: Path,
    candidates: Path,
    rides: Path | None,
    gpx: Path | None,
    seed: int,
) -> dict:
    rows = pq.read_table(anchors).to_pylist()
    loaded = load_profiles(profiles, candidates)
    structure_table = pq.read_table(candidates, columns=["candidate_id", "structure"])
    structures = {
        c: s
        for c, s in zip(
            structure_table.column("candidate_id").to_pylist(),
            structure_table.column("structure").to_pylist(),
            strict=True,
        )
        if s
    }

    def clip(row: Mapping) -> np.ndarray:
        clipped = profile_of(
            row["candidate_ids"], loaded, row["start_offset_m"], row["end_offset_m"]
        )
        if clipped is None:
            raise VerifyError(
                f"climb {row['climb_id']} clips to fewer than two samples — "
                "re-run krpaly_derive.anchor"
            )
        return np.array(clipped)

    # Only what is queued is kept: every climb's samples at once is a kraj's
    # worth of profile held twice.
    samples: dict[int, np.ndarray] = {}

    def samples_of(row: Mapping) -> np.ndarray:
        if row["climb_id"] not in samples:
            samples[row["climb_id"]] = clip(row)
        return samples[row["climb_id"]]

    rng = random.Random(seed)
    items: list[dict] = []
    population: dict[str, int] = {}

    ride_counts: Counter = Counter()
    if rides is not None:
        index = TopIndex(rows)
        cells = coverage_cells(loaded)
        for ride in read_rides(rides):
            if not (
                covered(cells, ride["start_lat"], ride["start_lon"])
                and covered(cells, ride["top_lat"], ride["top_lon"])
            ):
                ride_counts["uncovered"] += 1
                continue
            outcome, best = match_ride(ride, index)
            ride_counts[outcome] += 1
            track = np.empty((0, 4))
            if gpx is not None and (path := gpx / f"{ride['activity_id']}.gpx").is_file():
                track = track_segment(
                    read_track(path),
                    (ride["start_lat"], ride["start_lon"]),
                    (ride["top_lat"], ride["top_lon"]),
                )
            items.append(
                {
                    "id": f"ride:{ride['activity_id']}:{ride['climb_index']}",
                    "bucket": "ride",
                    "kind": "ride",
                    "why": {
                        "agrees": "resolves, and distance and gain agree",
                        "resolves": "resolves, but distance or gain disagree",
                        "top": "the top matches, the start does not",
                        "none": "nothing in the database resolves it",
                    }[outcome],
                    "choices": [list(c) for c in RIDE_CHOICES],
                    "auto": "found" if outcome == "agrees" else None,
                    "match": outcome,
                    "ride": {
                        "name": ride["name"],
                        "date": ride["date"],
                        "category": ride["category"],
                        "dist_m": round(ride["distance_m"], 1),
                        "gain_m": round(ride["elevation_m"], 1),
                        "start": [ride["start_lat"], ride["start_lon"]],
                        "top": [ride["top_lat"], ride["top_lon"]],
                        "samples": thinned(track),
                    },
                    "climbs": [
                        climb_json(r, samples_of(r), structures) for r in nearby(ride, index, best)
                    ],
                }
            )
        population["ride"] = sum(ride_counts.values())

    strata = Counter(row["category"] or "null" for row in rows)
    for stratum, row in stratified_sample(rows, RANDOM_PER_STRATUM, rng):
        item = climb_item("random", f"random, category {stratum}", [row], samples_of, structures)
        item["stratum"] = stratum
        items.append(item)
    population["random"] = len(rows)

    flagged: dict[str, list[tuple[Mapping, str]]] = defaultdict(list)
    for row in rows:
        for bucket, why in flags_of(row, clip(row), structures).items():
            flagged[bucket].append((row, why))
    for bucket in ("steep", "step", "shallow", "structure"):
        population[bucket] = len(flagged[bucket])
        for row, why in capped(flagged[bucket], FLAG_CAP, rng):
            items.append(climb_item(bucket, why, [row], samples_of, structures))

    pairs = duplicate_pairs(rows)
    population["duplicate"] = len(pairs)
    for first, second in capped(pairs, FLAG_CAP, rng):
        gap = metres(first["top_lat"], first["top_lon"], second["top_lat"], second["top_lon"])
        why = f"tops {gap:.0f} m apart"
        items.append(climb_item("duplicate", why, [first, second], samples_of, structures))

    # Samples are per climb and climbs are shared, so the id is what is unique.
    items.sort(key=lambda item: (BUCKETS.index(item["bucket"]), item["id"]))
    return {
        "anchors_sha256": sha256_of(anchors),
        "seed": seed,
        "population": population,
        "strata": dict(sorted(strata.items())),
        "rides": dict(sorted(ride_counts.items())),
        "items": items,
    }


def queue_stage(out: Path, rides: Path | None, gpx: Path | None, seed: int) -> int:
    paths = {name: out / name for name in (ANCHORS_NAME, PROFILES_NAME, CANDIDATES_NAME)}
    for path in paths.values():
        if not path.is_file():
            raise VerifyError(f"{path} is not a file — run the pipeline through anchor first")
    queue = build_queue(
        paths[ANCHORS_NAME], paths[PROFILES_NAME], paths[CANDIDATES_NAME], rides, gpx, seed
    )
    review = out / REVIEW_DIR
    review.mkdir(parents=True, exist_ok=True)
    write_atomically(review / QUEUE_NAME, json.dumps(queue, separators=(",", ":")).encode())

    queued = Counter(item["bucket"] for item in queue["items"])
    for bucket in BUCKETS:
        if bucket in queue["population"]:
            print(
                f"{bucket}: {queued[bucket]} queued of {queue['population'][bucket]}",
                file=sys.stderr,
            )
    if queue["rides"]:
        print("rides: " + ", ".join(f"{k} {v}" for k, v in queue["rides"].items()), file=sys.stderr)
    print(f"queue: {review / QUEUE_NAME}", file=sys.stderr)
    return 0


# --- verdicts ----------------------------------------------------------------


def read_queue(out: Path) -> dict:
    path = out / REVIEW_DIR / QUEUE_NAME
    try:
        return json.loads(path.read_text())
    except (OSError, json.JSONDecodeError) as error:
        raise VerifyError(f"{path} cannot be read — run `verify queue` first") from error


def read_verdicts(path: Path) -> dict[str, dict]:
    """The latest verdict per item. A torn last line is an interrupted append, not data."""
    latest: dict[str, dict] = {}
    if not path.is_file():
        return latest
    for line in path.read_text().splitlines():
        try:
            verdict = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(verdict, dict) and "id" in verdict and "verdict" in verdict:
            latest[verdict["id"]] = verdict
    return latest


def decided(queue: Mapping, verdicts: Mapping[str, dict]) -> list[tuple[dict, str, str]]:
    """Every item with a verdict — a person's, or the queue's own — as (item, verdict, note)."""
    out = []
    for item in queue["items"]:
        given = verdicts.get(item["id"])
        if given is not None:
            out.append((item, given["verdict"], given.get("note") or ""))
        elif item["auto"] is not None:
            out.append((item, item["auto"], ""))
    return out


def entry(verdict: str, reason: str | None, climb: Mapping) -> dict:
    return {
        "verdict": verdict,
        "reason": reason,
        "anchor": climb["anchor"],
        "start": [round(v, 6) for v in climb["start"]],
        "top": [round(v, 6) for v in climb["top"]],
        "dist_m": climb["dist_m"],
        "gain_m": climb["gain_m"],
    }


def golden_entries(decisions: Iterable[tuple[dict, str, str]]) -> list[dict]:
    """What the verdicts say about roads, one entry per anchor, later verdicts winning.

    A `missing` ride has no anchor to name, so it is kept by its ends and its
    figures — rounded, and with nothing about the ride that measured them.
    """
    by_key: dict[object, dict] = {}
    for item, verdict, _ in decisions:
        climbs = item["climbs"]
        if item["kind"] == "climb":
            if verdict == "climb":
                by_key[anchor_id_of(climbs[0])] = entry("keep", None, climbs[0])
            elif verdict in REJECTING:
                by_key[anchor_id_of(climbs[0])] = entry("reject", verdict, climbs[0])
        elif item["kind"] == "pair":
            if verdict == "duplicate":
                by_key[anchor_id_of(climbs[1])] = entry("reject", "duplicate", climbs[1])
        elif item["kind"] == "ride":
            if verdict == "found" and climbs:
                by_key[anchor_id_of(climbs[0])] = entry("keep", None, climbs[0])
            elif verdict == "bounds" and climbs:
                by_key[anchor_id_of(climbs[0])] = entry("reject", "bounds", climbs[0])
            elif verdict == "missing":
                ride = item["ride"]
                key = ("missing", *ride["start"], *ride["top"])
                by_key[key] = {
                    "verdict": "missing",
                    "reason": None,
                    "anchor": None,
                    "start": [round(v, 5) for v in ride["start"]],
                    "top": [round(v, 5) for v in ride["top"]],
                    "dist_m": round(ride["dist_m"]),
                    "gain_m": round(ride["gain_m"]),
                }
    return sorted(by_key.values(), key=lambda e: (e["verdict"], e["top"], e["start"]))


def anchor_id_of(climb: Mapping) -> str:
    a = climb["anchor"]
    return anchor_id((tuple(a["way_refs"]), a["start_node_id"], a["end_node_id"]))


def precision(queue: Mapping, decisions: Iterable[tuple[dict, str, str]]) -> dict | None:
    """The stratified estimate of how many climbs are real, off the random bucket only.

    Each stratum's share is weighted by its size in the derivation. A stratum
    nobody reviewed cannot be weighted, so it is left out and named, and the
    weights of the rest are renormalised — which is an assumption, and said.
    """
    tally: dict[str, Counter] = defaultdict(Counter)
    for item, verdict, _ in decisions:
        if item["bucket"] != "random" or verdict == "unsure":
            continue
        tally[item["stratum"]]["keep" if verdict == "climb" else "reject"] += 1
    reviewed = {s: t for s, t in tally.items() if sum(t.values())}
    if not reviewed:
        return None
    total = sum(queue["strata"][s] for s in reviewed)
    estimate = variance = 0.0
    for stratum, t in reviewed.items():
        n = sum(t.values())
        weight = queue["strata"][stratum] / total
        estimate += weight * t["keep"] / n
        # Agresti–Coull: two added successes and two failures, so a stratum
        # reviewed as all-real on fifteen rows still carries its uncertainty
        # rather than a variance of zero.
        adjusted = (t["keep"] + 2) / (n + 4)
        variance += weight**2 * adjusted * (1 - adjusted) / (n + 4)
    return {
        "estimate": estimate,
        "margin": 1.96 * math.sqrt(variance),
        "reviewed": sum(sum(t.values()) for t in reviewed.values()),
        "missing_strata": sorted(set(queue["strata"]) - set(reviewed)),
    }


def accept_stage(out: Path, verified: Path | None) -> int:
    queue = read_queue(out)
    verdicts = read_verdicts(out / REVIEW_DIR / VERDICTS_NAME)
    decisions = decided(queue, verdicts)

    table: dict[str, Counter] = defaultdict(Counter)
    for item, verdict, _ in decisions:
        table[item["bucket"]][verdict] += 1
    queued = Counter(item["bucket"] for item in queue["items"])
    for bucket in BUCKETS:
        if queued[bucket]:
            split = ", ".join(f"{v} {n}" for v, n in table[bucket].most_common())
            print(
                f"{bucket}: {sum(table[bucket].values())}/{queued[bucket]} decided"
                + (f" — {split}" if split else ""),
                file=sys.stderr,
            )

    estimate = precision(queue, decisions)
    if estimate is not None:
        note = (
            f", strata not reviewed: {', '.join(estimate['missing_strata'])}"
            if estimate["missing_strata"]
            else ""
        )
        print(
            f"real climbs: {estimate['estimate'] * 100:.0f} % ± {estimate['margin'] * 100:.0f} "
            f"(95 %, {estimate['reviewed']} random rows{note})",
            file=sys.stderr,
        )
    if queue["rides"]:
        found = table["ride"]["found"] + table["ride"]["bounds"]
        counted = found + table["ride"]["missing"]
        if counted:
            print(
                f"ridden climbs found: {found}/{counted} "
                f"({table['ride']['bounds']} with wrong bounds)",
                file=sys.stderr,
            )

    entries = golden_entries(decisions)
    path = verified or VERIFIED_ROOT / f"{out.resolve().name}.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    document = {
        "verified_on": out.resolve().name,
        "anchors_sha256": queue["anchors_sha256"],
        "counts": dict(sorted(Counter(e["verdict"] for e in entries).items())),
        "climbs": entries,
    }
    write_atomically(path, (json.dumps(document, indent=2) + "\n").encode())
    print(f"verified: {path}, {len(entries)} entries", file=sys.stderr)
    return 0


# --- check -------------------------------------------------------------------


def check_entries(entries: Iterable[Mapping], rows: Sequence[Mapping]) -> list[tuple[str, dict]]:
    """Each golden entry against a derivation, as (outcome, entry).

    Outcomes: `kept`, `drifted` (kept, distance or gain moved by more than a
    tenth), `moved` (the anchor is gone but the resolution API still finds a
    climb there), `lost`; `gone` and `still_there` for rejections; `found` and
    `still_missing` for climbs a ride proved missing.
    """
    by_anchor = {anchor_of(row): row for row in rows}
    index = TopIndex(rows)
    results = []
    for e in entries:
        anchor = e["anchor"]
        exact = (
            by_anchor.get(
                (tuple(anchor["way_refs"]), anchor["start_node_id"], anchor["end_node_id"])
            )
            if anchor
            else None
        )
        resolved = index.resolve(e["start"], e["top"])
        if e["verdict"] == "keep":
            if exact is not None:
                drifted = not (
                    within(exact["dist_m"], e["dist_m"], 0.1)
                    and within(exact["gain_m"], e["gain_m"], 0.1)
                )
                results.append(("drifted" if drifted else "kept", e))
            else:
                results.append(("moved" if resolved else "lost", e))
        elif e["verdict"] == "reject":
            results.append(("still_there" if exact is not None else "gone", e))
        else:
            results.append(("found" if resolved else "still_missing", e))
    return results


REGRESSIONS = frozenset({"lost", "still_there"})


def check_stage(out: Path, verified: Path) -> int:
    anchors = out / ANCHORS_NAME
    if not anchors.is_file():
        raise VerifyError(f"{anchors} is not a file — run krpaly_derive.anchor first")
    try:
        document = json.loads(verified.read_text())
    except (OSError, json.JSONDecodeError) as error:
        raise VerifyError(f"{verified} cannot be read — run `verify accept` first") from error
    rows = pq.read_table(anchors).to_pylist()
    results = check_entries(document["climbs"], rows)
    counts = Counter(outcome for outcome, _ in results)
    print(
        f"against {verified.name}: " + ", ".join(f"{k} {v}" for k, v in sorted(counts.items())),
        file=sys.stderr,
    )
    for outcome, e in results:
        if outcome in REGRESSIONS or outcome == "drifted":
            print(
                f"  {outcome}: top {e['top'][0]:.5f},{e['top'][1]:.5f} "
                f"{e['dist_m']:.0f} m / {e['gain_m']:.0f} m"
                + (f" ({e['reason']})" if e["reason"] else ""),
                file=sys.stderr,
            )
    return 1 if REGRESSIONS & counts.keys() else 0


# --- serve -------------------------------------------------------------------


def handler_for(out: Path) -> type[BaseHTTPRequestHandler]:
    """The review page, its queue, and an append-only verdict log, on one directory."""
    review = out / REVIEW_DIR
    queue = read_queue(out)
    choices = {item["id"]: {c[0] for c in item["choices"]} for item in queue["items"]}
    body = json.dumps(queue, separators=(",", ":")).encode()

    class Handler(BaseHTTPRequestHandler):
        def send(self, status: HTTPStatus, data: bytes, content_type: str) -> None:
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(data)

        def do_GET(self) -> None:  # noqa: N802 — the stdlib's name
            if self.path == "/":
                self.send(HTTPStatus.OK, PAGE.read_bytes(), "text/html; charset=utf-8")
            elif self.path == "/queue.json":
                self.send(HTTPStatus.OK, body, "application/json")
            elif self.path == "/verdicts.json":
                latest = read_verdicts(review / VERDICTS_NAME)
                self.send(HTTPStatus.OK, json.dumps(latest).encode(), "application/json")
            else:
                self.send(HTTPStatus.NOT_FOUND, b"not found", "text/plain")

        def do_POST(self) -> None:  # noqa: N802
            if self.path != "/verdict":
                self.send(HTTPStatus.NOT_FOUND, b"not found", "text/plain")
                return
            try:
                length = int(self.headers.get("Content-Length", "0"))
                posted = json.loads(self.rfile.read(length))
                item_id, verdict = posted["id"], posted["verdict"]
                note = str(posted.get("note") or "")[:500]
            except (ValueError, KeyError, TypeError):
                self.send(HTTPStatus.BAD_REQUEST, b"want {id, verdict, note}", "text/plain")
                return
            if verdict not in choices.get(item_id, ()):
                self.send(HTTPStatus.BAD_REQUEST, b"no such item or verdict", "text/plain")
                return
            line = {
                "id": item_id,
                "verdict": verdict,
                "note": note,
                "at": datetime.now(UTC).isoformat(timespec="seconds"),
            }
            with (review / VERDICTS_NAME).open("a") as log:
                log.write(json.dumps(line) + "\n")
            self.send(HTTPStatus.OK, json.dumps(line).encode(), "application/json")

        def log_message(self, format: str, *args: object) -> None:  # noqa: A002
            return

    return Handler


def serve_stage(out: Path, port: int) -> int:
    server = ThreadingHTTPServer(("127.0.0.1", port), handler_for(out))
    print(f"review: http://127.0.0.1:{port}/ — Ctrl-C to stop", file=sys.stderr)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="krpaly_derive.verify", description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)

    queue = commands.add_parser("queue", help="choose what a person should look at")
    queue.add_argument("--out", required=True, type=Path, help="a stage directory #10 wrote")
    queue.add_argument("--rides", type=Path, help="a ~/sport garmin.db to match ridden climbs")
    queue.add_argument("--gpx", type=Path, help="the directory of <activity_id>.gpx tracks")
    queue.add_argument("--seed", type=int, default=1, help="for the random and capped buckets")

    serve = commands.add_parser("serve", help="the review page, on localhost")
    serve.add_argument("--out", required=True, type=Path)
    serve.add_argument("--port", type=int, default=8765)

    accept = commands.add_parser("accept", help="summarise verdicts into the golden set")
    accept.add_argument("--out", required=True, type=Path)
    accept.add_argument(
        "--verified",
        type=Path,
        help="where to write (default: derive/verified/<name of --out>.json)",
    )

    check = commands.add_parser("check", help="hold a derivation to a golden set")
    check.add_argument("--out", required=True, type=Path, help="the derivation to check")
    check.add_argument("--verified", required=True, type=Path, help="a verified/*.json")

    args = parser.parse_args(argv)
    try:
        if args.command == "queue":
            if args.gpx is not None and args.rides is None:
                raise VerifyError("--gpx draws ride tracks, and needs --rides")
            return queue_stage(args.out, args.rides, args.gpx, args.seed)
        if args.command == "serve":
            return serve_stage(args.out, args.port)
        if args.command == "accept":
            return accept_stage(args.out, args.verified)
        return check_stage(args.out, args.verified)
    except VerifyError as error:
        raise SystemExit(f"verify: {error}") from error


if __name__ == "__main__":
    raise SystemExit(main())
