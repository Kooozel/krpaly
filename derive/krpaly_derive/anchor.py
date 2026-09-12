"""Stage 5: climbs in, anchored and deduped climbs out.

    uv run --directory derive python -m krpaly_derive.anchor --out data/kraj-1-v2

#9 emits a climb per detection, and once runs chain, one real climb is detected
several times over overlapping candidate ranges. This is where a climb stops
being a position in an emission order and becomes an identity: **the ordered
way sequence plus the junction nodes at either end**, which is what
`climb_anchor_unique` is declared on.

* **The anchor is a fact about the world**, not about the detector. The key it
  replaces — rounded summit plus rounded gain — hashes an engine *output*, and
  on 115 rows in `~/sport` it both splits one hill into three identities and
  fuses two climbs at a shared summit. A way sequence survives a retune.
* **Junction nodes, not the nearest node to each end.** The anchor then moves
  only when a climb end crosses a junction, not when a retune shifts a start
  200 m — and two roads converging below a summit share an `end_node_id`, so
  approach variants group by a column that already exists.
* **Consecutive repeats in `way_refs` collapse.** How many pieces a way was cut
  into depends on the degree of its nodes, which changes when an unrelated way
  is added anywhere in Czechia. The way *sequence* does not.
* **Approach direction is already in the anchor**, in the order of `way_refs`
  and in which node is the start — so there is no direction column here, as
  there is none on `climb`. A summit reached from two sides is two rows.

It writes Parquet, not Postgres (#11 loads), and it does not detect anything.
"""

from __future__ import annotations

import argparse
import bisect
import json
import sys
import time
from collections import Counter, defaultdict
from datetime import UTC, datetime
from pathlib import Path

import pyarrow as pa
import pyarrow.parquet as pq

from krpaly_derive.dem import read_manifest
from krpaly_derive.detect import MANIFEST_NAME as CLIMBS_MANIFEST
from krpaly_derive.detect import OUTPUT_NAME as CLIMBS_NAME
from krpaly_derive.extract import OUTPUT_NAME as CANDIDATES_NAME
from krpaly_derive.extract import already_done, sha256_of, write_table
from krpaly_derive.record import RecordError, record_dir, write_atomically, write_record
from krpaly_derive.sample import OUTPUT_NAME as PROFILES_NAME

OUTPUT_NAME = "anchors.parquet"
MANIFEST_NAME = "anchors.manifest.json"

# In the stage signature: a changed anchoring or dedupe rule re-derives rather
# than finding an anchors.parquet "already anchored from these inputs".
POLICY_VERSION = 1

# A climb end within this of a junction is treated as being at it. The samples
# are 10 m apart (#8's DEFAULT_STEP_M) and the engine reports a position along
# the profile it was given, so anything finer is noise, not intent.
JUNCTION_TOLERANCE_M = 1.0

SCHEMA = pa.schema(
    [
        # Position in emission order, as candidate_id and run_id are.
        ("climb_id", pa.uint64()),
        # The anchor: the ordered way sequence, consecutive repeats collapsed.
        ("way_refs", pa.list_(pa.int64())),
        # The other half of the anchor — the covered range's outer junctions.
        ("start_node_id", pa.int64()),
        ("end_node_id", pa.int64()),
        # The candidates the anchor spans, which is what #11 rebuilds the
        # geometry from.
        ("candidate_ids", pa.list_(pa.uint64())),
        # Where detection opened and closed inside that range, measured from
        # its start: #11 can clip the profile to the climb or keep the whole
        # range, without re-deriving either.
        ("start_offset_m", pa.float64()),
        ("end_offset_m", pa.float64()),
        # Carried from #9 unchanged. Both grades are already per cent there.
        ("dist_m", pa.float64()),
        ("gain_m", pa.float64()),
        ("avg_grade_pct", pa.float64()),
        ("max_grade_pct", pa.float64()),
        ("start_lat", pa.float64()),
        ("start_lon", pa.float64()),
        ("top_lat", pa.float64()),
        ("top_lon", pa.float64()),
        ("difficulty", pa.float64()),
        ("category", pa.string()),
        # Which detection won, so a surprising row can be traced to its run.
        ("run_id", pa.uint64()),
        ("climb_index", pa.int32()),
        # How many detections this row absorbed.
        ("collapsed", pa.int32()),
    ]
)

# 1..9 exactly, then one bucket: a histogram to read, not to plot.
BUCKET_MAX = 9


class AnchorError(Exception):
    """An input this stage cannot anchor, said in one line."""


def read_climbs_manifest(climbs: Path, profiles: Path, candidates: Path) -> dict[str, str]:
    """All three inputs' sha256, refused unless #9's manifest vouches for them.

    #9's manifest is the one place that says which profiles and which
    candidates a `climbs.parquet` was detected from. Anchoring a climb against
    a different extraction would map its distances onto candidates that were
    never under it.
    """
    path = climbs.parent / CLIMBS_MANIFEST
    manifest = read_manifest(path)
    digests = {
        "climbs_sha256": sha256_of(climbs),
        "profiles_sha256": sha256_of(profiles),
        "candidates_sha256": sha256_of(candidates),
    }
    if manifest.get("output", {}).get("sha256") != digests["climbs_sha256"]:
        raise AnchorError(f"{climbs} is not the file {path} records — re-run krpaly_derive.detect")
    source = manifest.get("source", {})
    for name, path_of in (("profiles", profiles), ("candidates", candidates)):
        if source.get(f"{name}_sha256") != digests[f"{name}_sha256"]:
            raise AnchorError(
                f"{climbs} was detected from other {name} than {path_of} — "
                "re-run krpaly_derive.detect"
            )
    return digests


def candidate_lengths(profiles: Path) -> dict[int, float]:
    """Each candidate's length along its own profile, by id.

    The same figure `join_profiles` accumulates — `distance_m[-1] −
    distance_m[0]`, the shared junction vertex never counted twice — so these
    offsets and a climb's distances are on the same ruler. Read off the flat
    list values rather than through a Python list per candidate: a kraj is
    millions of samples and only the two ends of each are wanted.
    """
    table = pq.read_table(profiles, columns=["candidate_id", "distance_m"])
    column = table.column("distance_m").combine_chunks()
    offsets = column.offsets.to_numpy()
    values = column.flatten().to_numpy()
    offsets = offsets - offsets[0]
    ends = values[offsets[1:] - 1] - values[offsets[:-1]]
    return dict(zip(table.column("candidate_id").to_pylist(), ends.tolist(), strict=True))


def candidate_anchors(candidates: Path) -> dict[int, tuple[list[int], int, int]]:
    """Each candidate's way refs and its two junction nodes, by id."""
    table = pq.read_table(
        candidates, columns=["candidate_id", "way_refs", "start_node_id", "end_node_id"]
    )
    return dict(
        zip(
            table.column("candidate_id").to_pylist(),
            zip(
                table.column("way_refs").to_pylist(),
                table.column("start_node_id").to_pylist(),
                table.column("end_node_id").to_pylist(),
                strict=True,
            ),
            strict=True,
        )
    )


def collapse(way_refs: list[int]) -> list[int]:
    """Consecutive repeats dropped: a way cut into three pieces is one way."""
    kept: list[int] = []
    for way in way_refs:
        if not kept or kept[-1] != way:
            kept.append(way)
    return kept


def covered_range(run: list[int], lengths: dict[int, float], start: float, end: float) -> tuple:
    """The contiguous candidate range a climb falls in, and where in it it sits.

    `cumulative[i]` is where candidate `run[i]` starts along the joined
    profile, so the climb's start lies in the last candidate that begins at or
    before it and its end in the first that reaches it. A climb ending exactly
    on a junction ends with the candidate before that junction, which is what
    the tolerance is for.
    """
    cumulative = [0.0]
    for candidate_id in run:
        cumulative.append(cumulative[-1] + lengths[candidate_id])
    first = min(max(bisect.bisect_right(cumulative, start) - 1, 0), len(run) - 1)
    last = bisect.bisect_left(cumulative, end - JUNCTION_TOLERANCE_M, 1) - 1
    last = min(max(last, first), len(run) - 1)
    return run[first : last + 1], start - cumulative[first], end - cumulative[first]


def anchored(climbs: pa.Table, lengths: dict[int, float], anchors: dict) -> list[dict]:
    """Every detection as a row of SCHEMA, minus the dedupe and the climb_id."""
    rows = []
    for climb in climbs.to_pylist():
        run = climb["candidate_ids"]
        missing = [c for c in run if c not in lengths or c not in anchors]
        if missing:
            raise AnchorError(
                f"run {climb['run_id']} names candidate {missing[0]}, which has no "
                "profile or is not among these candidates — re-run krpaly_derive.detect"
            )
        chain, start_offset, end_offset = covered_range(
            run, lengths, climb["start_distance_m"], climb["end_distance_m"]
        )
        way_refs = collapse([way for c in chain for way in anchors[c][0]])
        rows.append(
            {
                "way_refs": way_refs,
                "start_node_id": anchors[chain[0]][1],
                "end_node_id": anchors[chain[-1]][2],
                "candidate_ids": list(chain),
                "start_offset_m": start_offset,
                "end_offset_m": end_offset,
                **{
                    key: climb[key]
                    for key in (
                        "dist_m",
                        "gain_m",
                        "avg_grade_pct",
                        "max_grade_pct",
                        "start_lat",
                        "start_lon",
                        "top_lat",
                        "top_lon",
                        "difficulty",
                        "category",
                        "run_id",
                        "climb_index",
                    )
                },
                "collapsed": 0,
            }
        )
    return rows


def _better(row: dict) -> tuple:
    """Ordering within a collision: the longer chain, then the greater gain.

    The tie after that is `(run_id, climb_index)`, so which detection wins is
    a property of the input rather than of the iteration order.
    """
    return (-len(row["candidate_ids"]), -row["gain_m"], row["run_id"], row["climb_index"])


def dedupe(rows: list[dict]) -> tuple[list[dict], int]:
    """One row per climb, and how many detections were absorbed into them.

    Grouped by the last covered candidate — the summit-bearing one — so a
    chain contained in another is exactly a **suffix** of it: the same road,
    opened later because the run started higher, and the longer detection
    wins. Chains that overlap without one containing the other are left alone:
    with the anchor quantized to junctions that means they fork below the
    summit, which is two approach variants sharing a tail, not an artefact.
    """
    groups: dict[int, list[dict]] = defaultdict(list)
    for row in rows:
        groups[row["candidate_ids"][-1]].append(row)

    kept: list[dict] = []
    collapsed = 0
    for summit in sorted(groups):
        winners: list[dict] = []
        for row in sorted(groups[summit], key=_better):
            chain = tuple(row["candidate_ids"])
            into = next(
                (
                    winner
                    for winner in winners
                    if tuple(winner["candidate_ids"])[-len(chain) :] == chain
                ),
                None,
            )
            if into is None:
                winners.append(row)
            else:
                into["collapsed"] += 1 + row["collapsed"]
                collapsed += 1
        kept.extend(winners)
    return kept, collapsed


def unique_anchors(rows: list[dict]) -> tuple[list[dict], int]:
    """The invariant #11 is held to: one row per `climb_anchor_unique` key.

    Two chains that are not suffixes of each other can still collapse onto the
    same way sequence between the same two junctions, and the load would then
    fail on the constraint rather than here. Counted separately from the
    dedupe's own collapses, because many of them would mean the anchor itself
    is too coarse — which is worth knowing now rather than after #11.
    """
    winners: dict[tuple, dict] = {}
    collisions = 0
    for row in sorted(rows, key=_better):
        key = (tuple(row["way_refs"]), row["start_node_id"], row["end_node_id"])
        if key in winners:
            winners[key]["collapsed"] += 1 + row["collapsed"]
            collisions += 1
        else:
            winners[key] = row
    kept = sorted(winners.values(), key=lambda row: (row["run_id"], row["climb_index"]))
    for climb_id, row in enumerate(kept):
        row["climb_id"] = climb_id
    return kept, collisions


def counts_of(rows: list[dict], detections: int, collapsed: int, collisions: int) -> dict:
    """The four counts #10 owes, plus what makes them readable."""
    per_summit = Counter(row["end_node_id"] for row in rows)
    sizes = Counter(len(row["candidate_ids"]) for row in rows)
    by_candidates = {str(n): sizes.get(n, 0) for n in range(1, BUCKET_MAX + 1)}
    by_candidates[f"{BUCKET_MAX + 1}+"] = sum(n for size, n in sizes.items() if size > BUCKET_MAX)
    by_category: dict[str, int] = defaultdict(int)
    for row in rows:
        by_category[row["category"] or "null"] += 1
    return {
        "detections": detections,
        "climbs": len(rows),
        "collapsed": collapsed,
        "anchor_collisions": collisions,
        "summits_multi": sum(1 for n in per_summit.values() if n > 1),
        "multi_candidate": sum(1 for row in rows if len(row["candidate_ids"]) > 1),
        "by_candidates": by_candidates,
        # climb_anchor_unique is a btree over an array, and db/README.md
        # measures its ceiling at 473 way ids.
        "max_way_refs": max((len(row["way_refs"]) for row in rows), default=0),
        "by_category": dict(sorted(by_category.items())),
    }


def stage_signature(digests: dict[str, str]) -> dict[tuple[str, str], object]:
    """Everything about a run that changes what comes out of it, as #6 keys it."""
    return {("source", key): value for key, value in digests.items()} | {
        ("anchor", "policy_version"): POLICY_VERSION,
        ("anchor", "junction_tolerance_m"): JUNCTION_TOLERANCE_M,
    }


def anchor_keys(path: Path) -> set[tuple]:
    """One `climb_anchor_unique` key per row of an anchors.parquet."""
    table = pq.read_table(path, columns=["way_refs", "start_node_id", "end_node_id"])
    return {
        (tuple(way_refs), start, end)
        for way_refs, start, end in zip(
            table.column("way_refs").to_pylist(),
            table.column("start_node_id").to_pylist(),
            table.column("end_node_id").to_pylist(),
            strict=True,
        )
    }


def compare(output_path: Path, other: Path) -> None:
    """How many anchors survived a retune, on stderr only.

    It is an experiment, not provenance, so nothing about it enters the
    manifest: the comparison is between two derivations, and each already
    records what it was.
    """
    if not other.is_file():
        raise AnchorError(f"{other} is not a file — pass an anchors.parquet to --compare")
    mine, theirs = anchor_keys(output_path), anchor_keys(other)
    print(
        f"compared with {other.name}: {len(mine & theirs)} kept, {len(mine - theirs)} new, "
        f"{len(theirs - mine)} lost, of {len(theirs)} there and {len(mine)} here",
        file=sys.stderr,
    )


def report(written: dict) -> None:
    """What the dedupe did, on stderr, as detect.py reports."""
    counts = written["counts"]
    print(
        f"climbs: {counts['climbs']} from {counts['detections']} detections "
        f"({counts['collapsed']} collapsed, {counts['anchor_collisions']} anchor collisions)",
        file=sys.stderr,
    )
    print(
        f"anchors: {counts['multi_candidate']} over more than one candidate, "
        f"{counts['summits_multi']} summits with more than one climb, "
        f"longest way_refs {counts['max_way_refs']}",
        file=sys.stderr,
    )
    split = ", ".join(
        f"{'none' if category == 'null' else category} {n}"
        for category, n in counts["by_category"].items()
    )
    print(f"categories: {split}", file=sys.stderr)
    print(
        f"anchors: {written['output']['file']}, {written['run']['wall_clock_s']} s",
        file=sys.stderr,
    )


def anchor_stage(
    out: Path,
    climbs: Path,
    profiles: Path,
    candidates: Path,
    force: bool,
    record: Path | None = None,
    other: Path | None = None,
) -> int:
    for path, stage in ((climbs, "detect"), (profiles, "sample"), (candidates, "extract")):
        if not path.is_file():
            raise AnchorError(f"{path} is not a file — run krpaly_derive.{stage} first")

    out.mkdir(parents=True, exist_ok=True)
    output_path = out / OUTPUT_NAME
    manifest_path = out / MANIFEST_NAME
    record = record or record_dir(out)

    started = time.monotonic()
    started_at = datetime.now(UTC).isoformat(timespec="seconds")

    digests = read_climbs_manifest(climbs, profiles, candidates)
    signature = stage_signature(digests)
    if not force and already_done(manifest_path, output_path, signature):
        # Recorded from the prior manifest, as detect does, so a no-op re-run
        # still restores a deleted record.
        write_record(read_manifest(manifest_path), record, MANIFEST_NAME)
        print(
            f"{output_path} is already anchored from these inputs — pass --force to redo",
            file=sys.stderr,
        )
        if other is not None:
            compare(output_path, other)
        return 0

    detections = pq.read_table(climbs)
    rows = anchored(detections, candidate_lengths(profiles), candidate_anchors(candidates))
    kept, collapsed = dedupe(rows)
    kept, collisions = unique_anchors(kept)
    write_table(pa.Table.from_pylist(kept, schema=SCHEMA), output_path)

    manifest = {
        "source": {
            "climbs": climbs.name,
            "profiles": profiles.name,
            "candidates": candidates.name,
            **digests,
        },
        "anchor": {
            "policy_version": POLICY_VERSION,
            "junction_tolerance_m": JUNCTION_TOLERANCE_M,
        },
        "counts": counts_of(kept, detections.num_rows, collapsed, collisions),
        "output": {
            "file": OUTPUT_NAME,
            "sha256": sha256_of(output_path),
            "bytes": output_path.stat().st_size,
        },
        # The only block allowed to differ between two runs over the same input.
        "run": {
            "started_at": started_at,
            "wall_clock_s": round(time.monotonic() - started, 3),
        },
    }
    write_atomically(
        manifest_path, (json.dumps(manifest, indent=2, sort_keys=True) + "\n").encode("utf-8")
    )
    write_record(manifest, record, MANIFEST_NAME)
    report(manifest)
    if other is not None:
        compare(output_path, other)
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="krpaly_derive.anchor", description=__doc__)
    parser.add_argument("--out", required=True, type=Path, help="the stage directory #9 wrote")
    parser.add_argument(
        "--climbs",
        type=Path,
        default=None,
        help=f"climbs to anchor, beside #9's manifest (default: <out>/{CLIMBS_NAME})",
    )
    parser.add_argument(
        "--profiles",
        type=Path,
        default=None,
        help=f"the profiles they were detected over (default: <out>/{PROFILES_NAME})",
    )
    parser.add_argument(
        "--candidates",
        type=Path,
        default=None,
        help=f"the candidates carrying the anchors (default: <out>/{CANDIDATES_NAME})",
    )
    parser.add_argument(
        "--compare",
        type=Path,
        default=None,
        help="another anchors.parquet to report kept, new and lost anchors against, "
        "on stderr only — the §03 stability experiment, not provenance",
    )
    parser.add_argument(
        "--force", action="store_true", help="re-anchor even if the output is already current"
    )
    parser.add_argument(
        "--record",
        type=Path,
        default=None,
        help="where the committed copy goes (default: derive/manifests/<name of --out>)",
    )
    args = parser.parse_args(argv)

    try:
        return anchor_stage(
            out=args.out,
            climbs=args.climbs or args.out / CLIMBS_NAME,
            profiles=args.profiles or args.out / PROFILES_NAME,
            candidates=args.candidates or args.out / CANDIDATES_NAME,
            force=args.force,
            record=args.record,
            other=args.compare,
        )
    except (AnchorError, RecordError) as error:
        raise SystemExit(f"anchor: {error}") from error


if __name__ == "__main__":
    raise SystemExit(main())
