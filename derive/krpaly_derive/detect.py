"""Stage 4: engine-shaped profiles in, climbs out.

    uv run --directory derive python -m krpaly_derive.detect --out data/kraj-1

Where the derivation stops being geometry and becomes climbs. #8's profiles
are walked as **runs** — ordered chains of candidates that meet at a junction
— and every run goes through climb-engine's `detectClimbs` and is scored, in
one Node process for the whole batch (derive/engine/harness.mjs).

* **The library root, never climb-cli.** The CLI reads one GPX file and
  emits ride JSON a DEM profile has no inputs for; a process per candidate
  over a kraj is the wrong shape besides.
* **The build is read, never typed** (#1). The tag and commit come out of the
  vendored VERSION, and the library's sha256 sits beside them, so a file
  edited in place is visible in the manifest.
* **Every climb is kept**, `category = null` included. Null means the scoring
  model cleared no threshold, and §01's finding is that most climbs are like
  that: they are the product, not the noise.
* **Engine output enters krpaly here**, and so does its one unit trap:
  `maxSustainedGradient` is a fraction beside `avgGrade`'s per cent. It
  becomes per cent in this file, so no column of ours ever holds a fraction.

Each run is one candidate for now. Which chains are worth walking across a
junction is #10's decision — it is bound up with which candidate wins an
anchor — and `single_runs` is the seam it replaces. `join_profiles` already
takes a run of any length.

It does not dedupe (#10) or load anything (#11).
"""

from __future__ import annotations

import argparse
import contextlib
import json
import math
import re
import subprocess
import sys
import tempfile
import time
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import IO, NoReturn

import numpy as np
import pyarrow as pa
import pyarrow.parquet as pq

from krpaly_derive.dem import read_manifest
from krpaly_derive.extract import OUTPUT_NAME as SOURCE_NAME
from krpaly_derive.extract import already_done, sha256_of, write_table
from krpaly_derive.record import RecordError, record_dir, write_atomically, write_record
from krpaly_derive.sample import MANIFEST_NAME as PROFILES_MANIFEST
from krpaly_derive.sample import OUTPUT_NAME as PROFILES_NAME

OUTPUT_NAME = "climbs.parquet"
MANIFEST_NAME = "climbs.manifest.json"

DERIVE = Path(__file__).resolve().parent.parent
HARNESS = DERIVE / "engine" / "harness.mjs"
VENDOR = DERIVE / "vendor" / "climb-engine"
LIBRARY = VENDOR / "climb-engine.mjs"
VERSION_FILE = VENDOR / "VERSION"

# ~/sport's cycling model, so the first comparison against it is like for
# like. Recorded per derivation, so another model is a re-derivation rather
# than a migration.
SCORING_MODEL = "aso"

# What krpaly changes from the pinned build's DetectClimbsOptions defaults,
# which were tuned on GPS tracks. Empty until a comparison against ~/sport
# shows a default misreading a DEM profile; every key added here carries a
# comment saying which climb, and why. Python rather than a JSON file so that
# each value can carry its reason. The harness refuses an unknown key, a value
# that is not a finite number, and a key the engine derives from another at
# load (RESAMPLE_MIN_INTERVAL_M needs SPIKE_MAX_SEGMENT_M beside it, and
# CLIMB_LEADIN_GRADE_PCT needs TRIM_START_GRADE_PCT).
ENGINE_CONFIG_OVERRIDE: dict[str, float] = {}

# climb_category_known's set, checked here rather than at load: a stage that
# ran for an hour should not be refused afterwards for a fact it could see.
CATEGORIES = ("HC", "1", "2", "3", "4", "uncategorized")

VERSION_KEYS = ("tag", "source_commit", "vendored_on")
# `key: value`, where a key is one lower-case word. The prose paragraphs below
# the fields have colons too, but never after a single word at line start.
VERSION_LINE = re.compile(r"^([a-z_]+):\s+(.*?)\s*$")
SHA = re.compile(r"[0-9a-f]{40}")

# How long a harness whose stdin has closed gets to exit before it is killed.
# A healthy one finishes the line it is on and leaves at once.
CLOSE_TIMEOUT_S = 10

SCHEMA = pa.schema(
    [
        # Position in emission order, as candidate_id is.
        ("run_id", pa.uint64()),
        # The run itself: the candidates the profile was joined from, in
        # order. One element until #10 walks runs across junctions.
        ("candidate_ids", pa.list_(pa.uint64())),
        # The climb's position among its run's climbs, in order along it.
        ("climb_index", pa.int32()),
        # Where along the run's joined profile the climb starts and ends. The
        # engine reports no input indices, so these are the position — what
        # #10 maps back onto candidates, and so onto way_refs.
        ("start_distance_m", pa.float64()),
        ("end_distance_m", pa.float64()),
        ("dist_m", pa.float64()),
        ("gain_m", pa.float64()),
        ("avg_grade_pct", pa.float64()),
        # maxSustainedGradient × 100. Per cent, like avg_grade_pct beside it.
        ("max_grade_pct", pa.float64()),
        ("start_lat", pa.float64()),
        ("start_lon", pa.float64()),
        # endCoords, snapped to the summit by the engine.
        ("top_lat", pa.float64()),
        ("top_lon", pa.float64()),
        # Both null together when the scoring model cleared no threshold.
        ("difficulty", pa.float64()),
        ("category", pa.string()),
    ]
)

# A run is an ordered chain of candidate ids, each ending where the next one
# starts.
Run = tuple[int, ...]


class DetectError(Exception):
    """An input or an engine reply this stage cannot use, said in one line."""


@dataclass(frozen=True, eq=False)
class Profile:
    """One candidate's profile, #8's columns joined to #6's end nodes."""

    start_node_id: int
    end_node_id: int
    distance_m: np.ndarray
    elevation_m: np.ndarray
    lat: np.ndarray
    lon: np.ndarray


def read_version(path: Path = VERSION_FILE) -> dict[str, str]:
    """The vendored build's `key: value` fields, refused unless they pin it.

    The commit is held to the rule derivation_engine_commit_is_sha holds it
    to, so a VERSION that could never load fails here rather than at load.
    """
    fields: dict[str, str] = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        match = VERSION_LINE.match(line)
        if match is None:
            continue
        if match[1] in fields:
            raise DetectError(f"{path} gives {match[1]} twice — re-vendor rather than append")
        fields[match[1]] = match[2]
    missing = [key for key in VERSION_KEYS if not fields.get(key)]
    if missing:
        raise DetectError(
            f"{path} has no {', '.join(missing)} — re-vendor climb-engine as "
            "derive/INPUTS.md § Engine build says"
        )
    if not SHA.fullmatch(fields["source_commit"]):
        raise DetectError(
            f"{path}'s source_commit {fields['source_commit']!r} is not a full "
            "40-character hex SHA — a tag can be re-pointed, a SHA cannot"
        )
    return fields


def derivation_block(version: Mapping[str, str], override: dict, model: str) -> dict:
    """The manifest's `derivation` block, keyed exactly as the derivation columns.

    So #11 copies it into the row rather than mapping it.
    """
    return {
        "engine_version": version["tag"],
        "engine_commit": version["source_commit"],
        "engine_config_override": override,
        "scoring_model": model,
    }


def single_runs(candidate_ids: Iterable[int]) -> list[Run]:
    """One run per candidate, in the order given. The seam #10 replaces."""
    return [(int(candidate_id),) for candidate_id in candidate_ids]


def join_profiles(run: Run, profiles: Mapping[int, Profile]) -> list[list[float]]:
    """A run's candidates as one `[distance_m, elevation_m, lat, lon]` profile.

    Each later candidate's first sample is the previous one's last — the
    junction vertex both share — so it is dropped rather than repeated as a
    zero-length segment, and the candidate's distances are offset by the
    length of everything before it.
    """
    if not run:
        raise DetectError("an empty run has no profile")
    parts = []
    offset = 0.0
    previous: Profile | None = None
    for candidate_id in run:
        profile = profiles.get(candidate_id)
        if profile is None:
            raise DetectError(
                f"candidate {candidate_id} has no profile — #8 dropped it for nodata, "
                "or it is not among these candidates"
            )
        if previous is not None and previous.end_node_id != profile.start_node_id:
            raise DetectError(
                f"the run {run} breaks after node {previous.end_node_id}: candidate "
                f"{candidate_id} starts at node {profile.start_node_id}"
            )
        distance = profile.distance_m - profile.distance_m[0] + offset
        block = np.column_stack(
            [distance, profile.elevation_m.astype(np.float64), profile.lat, profile.lon]
        )
        parts.append(block if previous is None else block[1:])
        offset = float(distance[-1])
        previous = profile
    return np.concatenate(parts).tolist()


def load_profiles(profiles: Path, candidates: Path) -> dict[int, Profile]:
    """Every kept candidate's profile by id, in #8's order, with #6's end nodes.

    Flat arrays sliced per candidate rather than a Python list per sample: a
    kraj is millions of samples, and only the run being detected needs to be
    lists at all.
    """
    nodes = pq.read_table(candidates, columns=["candidate_id", "start_node_id", "end_node_id"])
    ends = dict(
        zip(
            nodes.column("candidate_id").to_pylist(),
            zip(
                nodes.column("start_node_id").to_pylist(),
                nodes.column("end_node_id").to_pylist(),
                strict=True,
            ),
            strict=True,
        )
    )

    table = pq.read_table(profiles)
    columns = {}
    for name in ("distance_m", "elevation_m", "lat", "lon"):
        column = table.column(name).combine_chunks()
        offsets = column.offsets.to_numpy()
        columns[name] = (column.flatten().to_numpy(), offsets - offsets[0])

    loaded = {}
    for i, candidate_id in enumerate(table.column("candidate_id").to_pylist()):
        if candidate_id not in ends:
            raise DetectError(f"candidate {candidate_id} has a profile but is not in {candidates}")
        start, end = ends[candidate_id]
        loaded[candidate_id] = Profile(
            start_node_id=start,
            end_node_id=end,
            **{name: values[o[i] : o[i + 1]] for name, (values, o) in columns.items()},
        )
    return loaded


class Harness:
    """One Node process for a whole batch, spoken to one line at a time.

    A context manager, so the process is reaped however the batch ends. Its
    stderr goes to a file rather than a pipe: nothing reads a pipe while
    Python waits on stdout, and an engine that ever wrote 64 KB of warnings
    would stall the pair. A file cannot fill up, and it is read once the
    process is gone.
    """

    def __init__(self, override: dict, model: str) -> None:
        # The one check made here rather than in the harness: JSON cannot
        # carry NaN or ∞ to it, so the harness would only ever see a parse
        # error that names nothing.
        for key, value in override.items():
            if isinstance(value, float) and not math.isfinite(value):
                raise DetectError(f"{key} is {value}, not a finite number")
        self.override = override
        self.model = model
        self.effective_config: dict = {}
        self.node = ""
        self._process: subprocess.Popen[str] | None = None
        self._errors: IO[str] | None = None
        self._stderr: str | None = None

    def __enter__(self) -> Harness:
        errors = tempfile.TemporaryFile(mode="w+", encoding="utf-8")
        try:
            self._process = subprocess.Popen(
                ["node", str(HARNESS)],
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=errors,
                text=True,
                encoding="utf-8",
            )
        except OSError as error:
            errors.close()
            if isinstance(error, FileNotFoundError):
                raise DetectError(
                    "node is not on PATH — the engine stage needs Node ≥20"
                ) from error
            raise DetectError(f"node could not be started: {error}") from error
        self._errors = errors
        try:
            engine = self._exchange({"config": self.override, "model": self.model}).get("engine")
            if not isinstance(engine, dict) or not {"effective_config", "node"} <= engine.keys():
                self._fail(f"the harness answered the header with {engine!r}, not its engine block")
        except BaseException:
            self._close()
            raise
        self.effective_config = engine["effective_config"]
        self.node = engine["node"]
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        stderr = self._close()
        if exc_type is None and self._process.returncode != 0:
            raise DetectError(
                f"the harness exited {self._process.returncode} after the batch: "
                f"{stderr.strip() or 'nothing on stderr'}"
            )

    def detect(self, run_id: int, points: list[list[float]]) -> list[dict]:
        """The run's climbs, scored — possibly none, which is still an answer."""
        reply = self._exchange({"id": run_id, "points": points})
        if reply.get("id") != run_id or not isinstance(reply.get("climbs"), list):
            self._fail(f"asked about run {run_id}, the harness answered {json.dumps(reply)[:120]}")
        return reply["climbs"]

    def _exchange(self, message: dict) -> dict:
        """One line out, one line back, and the line back is at least a JSON object."""
        process = self._process
        try:
            process.stdin.write(json.dumps(message, allow_nan=False) + "\n")
            process.stdin.flush()
        except BrokenPipeError:
            self._fail("the harness stopped reading")
        line = process.stdout.readline()
        if not line:
            self._fail("the harness closed its output without answering")
        try:
            reply = json.loads(line)
        except json.JSONDecodeError:
            self._fail(f"the harness answered with a line that is not JSON: {line[:80]!r}")
        if not isinstance(reply, dict):
            self._fail(f"the harness answered with {line[:80]!r}, not a JSON object")
        return reply

    def _fail(self, what: str) -> NoReturn:
        stderr = self._close()
        raise DetectError(
            f"{what} (node exited {self._process.returncode}): "
            f"{stderr.strip() or 'nothing on stderr'}"
        )

    def _close(self) -> str:
        """End the process and return what it said on stderr. Safe to call twice."""
        if self._stderr is None:
            process = self._process
            with contextlib.suppress(BrokenPipeError):
                process.stdin.close()
            process.stdout.close()
            try:
                process.wait(timeout=CLOSE_TIMEOUT_S)
            except subprocess.TimeoutExpired:
                # Its stdin is closed, so a harness still running is stuck
                # inside the engine; nothing it could still say is wanted.
                process.kill()
                process.wait()
            self._errors.seek(0)
            self._stderr = self._errors.read()
            self._errors.close()
        return self._stderr


def climb_rows(run_id: int, run: Run, climbs: list[dict]) -> list[dict]:
    """The harness's climbs for one run as rows of SCHEMA."""
    rows = []
    for index, climb in enumerate(climbs):
        for field in ("markerCoords", "endCoords"):
            if climb[field] is None:
                raise DetectError(
                    f"run {run_id}, climb {index}: the engine gave no {field}, though every "
                    "point of the profile has a position"
                )
        if climb["category"] is not None and climb["category"] not in CATEGORIES:
            raise DetectError(
                f"run {run_id}, climb {index}: category {climb['category']!r} is not one "
                "climb_category_known accepts"
            )
        rows.append(
            {
                "run_id": run_id,
                "candidate_ids": list(run),
                "climb_index": index,
                "start_distance_m": climb["startDistance"],
                "end_distance_m": climb["endDistance"],
                "dist_m": climb["distance"],
                "gain_m": climb["elevation"],
                "avg_grade_pct": climb["avgGrade"],
                "max_grade_pct": climb["maxSustainedGradient"] * 100,
                "start_lat": climb["markerCoords"]["lat"],
                "start_lon": climb["markerCoords"]["lon"],
                "top_lat": climb["endCoords"]["lat"],
                "top_lon": climb["endCoords"]["lon"],
                "difficulty": climb["difficulty"],
                "category": climb["category"],
            }
        )
    return rows


def read_profiles_manifest(profiles: Path, candidates: Path) -> tuple[str, str]:
    """Both inputs' sha256, refused unless #8's manifest vouches for the pair.

    The profiles carry no node ids, so they are joined to #6's candidates for
    them — and a join against a different extraction would chain runs
    through junctions that are not there.
    """
    path = profiles.parent / PROFILES_MANIFEST
    manifest = read_manifest(path)
    profiles_sha256 = sha256_of(profiles)
    if manifest.get("output", {}).get("sha256") != profiles_sha256:
        raise DetectError(
            f"{profiles} is not the file {path} records — re-run krpaly_derive.sample"
        )
    candidates_sha256 = sha256_of(candidates)
    if manifest.get("source", {}).get("sha256") != candidates_sha256:
        raise DetectError(
            f"{profiles} was sampled from other candidates than {candidates} — "
            "re-run krpaly_derive.sample"
        )
    return profiles_sha256, candidates_sha256


def stage_signature(
    profiles_sha256: str,
    candidates_sha256: str,
    library_sha256: str,
    harness_sha256: str,
    override: dict,
    model: str,
) -> dict[tuple[str, str], object]:
    """Everything about a run that changes what comes out of it, keyed as #6 keys it.

    A retune or a re-vendor re-derives; an unchanged re-run is a no-op.
    """
    return {
        ("source", "profiles_sha256"): profiles_sha256,
        ("source", "candidates_sha256"): candidates_sha256,
        ("engine", "library_sha256"): library_sha256,
        ("engine", "harness_sha256"): harness_sha256,
        ("derivation", "engine_config_override"): override,
        ("derivation", "scoring_model"): model,
    }


def report(written: dict) -> None:
    """What the engine made of the profiles, on stderr, as sample.py reports."""
    counts = written["counts"]
    derivation = written["derivation"]
    print(
        f"climbs: {counts['climbs']} over {counts['runs']} runs, "
        f"{counts['runs_with_climbs']} with at least one",
        file=sys.stderr,
    )
    split = ", ".join(
        f"{'none' if category == 'null' else category} {n}"
        for category, n in counts["by_category"].items()
    )
    print(f"categories ({derivation['scoring_model']}): {split}", file=sys.stderr)
    override = derivation["engine_config_override"] or "none"
    print(
        f"engine: {derivation['engine_version']} ({derivation['engine_commit'][:7]}), "
        f"override {override}",
        file=sys.stderr,
    )
    print(
        f"climbs: {written['output']['file']}, {written['run']['wall_clock_s']} s",
        file=sys.stderr,
    )


def detect_stage(
    out: Path,
    profiles: Path,
    candidates: Path,
    override: dict,
    model: str,
    force: bool,
    record: Path | None = None,
) -> int:
    if not profiles.is_file():
        raise DetectError(
            f"{profiles} is not a file — run krpaly_derive.sample first, or pass --profiles"
        )
    if not candidates.is_file():
        raise DetectError(
            f"{candidates} is not a file — run krpaly_derive.extract first, or pass --candidates"
        )
    version = read_version()

    out.mkdir(parents=True, exist_ok=True)
    output_path = out / OUTPUT_NAME
    manifest_path = out / MANIFEST_NAME
    record = record or record_dir(out)

    started = time.monotonic()
    started_at = datetime.now(UTC).isoformat(timespec="seconds")

    profiles_sha256, candidates_sha256 = read_profiles_manifest(profiles, candidates)
    library_sha256 = sha256_of(LIBRARY)
    harness_sha256 = sha256_of(HARNESS)
    signature = stage_signature(
        profiles_sha256, candidates_sha256, library_sha256, harness_sha256, override, model
    )
    if not force and already_done(manifest_path, output_path, signature):
        # Recorded from the prior manifest, as extract does, so a no-op re-run
        # still restores a deleted record.
        write_record(read_manifest(manifest_path), record, MANIFEST_NAME)
        print(
            f"{output_path} is already detected from these inputs — pass --force to redo",
            file=sys.stderr,
        )
        return 0

    loaded = load_profiles(profiles, candidates)
    runs = single_runs(loaded)
    rows: list[dict] = []
    runs_with_climbs = 0
    with Harness(override, model) as harness:
        for run_id, run in enumerate(runs):
            climbs = harness.detect(run_id, join_profiles(run, loaded))
            runs_with_climbs += bool(climbs)
            rows.extend(climb_rows(run_id, run, climbs))
    write_table(pa.Table.from_pylist(rows, schema=SCHEMA), output_path)

    by_category = dict.fromkeys([*CATEGORIES, "null"], 0)
    for row in rows:
        by_category[row["category"] or "null"] += 1

    manifest = {
        "source": {
            "profiles": profiles.name,
            "profiles_sha256": profiles_sha256,
            "candidates": candidates.name,
            "candidates_sha256": candidates_sha256,
        },
        "engine": {
            "tag": version["tag"],
            "source_commit": version["source_commit"],
            "vendored_on": version["vendored_on"],
            # A vendored file edited in place keeps its VERSION; it does not
            # keep its digest.
            "library_sha256": library_sha256,
            "harness_sha256": harness_sha256,
            "node": harness.node,
            "effective_config": harness.effective_config,
        },
        "derivation": derivation_block(version, override, model),
        "counts": {
            "runs": len(runs),
            "runs_with_climbs": runs_with_climbs,
            "climbs": len(rows),
            "by_category": by_category,
        },
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
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="krpaly_derive.detect", description=__doc__)
    parser.add_argument(
        "--out", required=True, type=Path, help="the stage directory #6 and #8 wrote"
    )
    parser.add_argument(
        "--profiles",
        type=Path,
        default=None,
        help=f"profiles to detect over, beside #8's manifest (default: <out>/{PROFILES_NAME})",
    )
    parser.add_argument(
        "--candidates",
        type=Path,
        default=None,
        help=f"the candidates they were sampled from (default: <out>/{SOURCE_NAME})",
    )
    parser.add_argument(
        "--force", action="store_true", help="re-detect even if the output is already current"
    )
    parser.add_argument(
        "--record",
        type=Path,
        default=None,
        help="where the committed copy goes (default: derive/manifests/<name of --out>)",
    )
    args = parser.parse_args(argv)

    try:
        return detect_stage(
            out=args.out,
            profiles=args.profiles or args.out / PROFILES_NAME,
            candidates=args.candidates or args.out / SOURCE_NAME,
            override=ENGINE_CONFIG_OVERRIDE,
            model=SCORING_MODEL,
            force=args.force,
            record=args.record,
        )
    except (DetectError, RecordError) as error:
        raise SystemExit(f"detect: {error}") from error


if __name__ == "__main__":
    raise SystemExit(main())
