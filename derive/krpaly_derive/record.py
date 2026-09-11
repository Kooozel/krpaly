"""The committed copy of a stage manifest.

Every stage writes its manifest under `--out`, which is gitignored and is the
stage's resume state. The record is that manifest minus `run` — the only block
allowed to differ between two runs over the same input — written to the tracked
`derive/manifests/<name of --out>/`. A re-run that changed nothing rewrites the
same bytes, so git sees no diff, and #11 hashes this file rather than the
working copy.

A module of its own because `extract` writes one too, and `dem` already imports
`extract`.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

# Resolved from the package rather than the working directory, so a run from
# `derive/` and a run from the repo root record to the same place.
RECORD_ROOT = Path(__file__).resolve().parents[1] / "manifests"


class RecordError(Exception):
    """A manifest that cannot be committed as it stands."""


def write_atomically(path: Path, data: bytes) -> None:
    """Through a temporary file and `os.replace`, as `write_parquet` does.

    An interrupted write must leave nothing half-written: a truncated tile still
    passes a check for its existence, and a truncated manifest or record is no
    provenance at all.
    """
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_bytes(data)
    os.replace(tmp, path)


def record_dir(out: Path) -> Path:
    """Where a run records to: named after its `--out`, so `data/kraj-1` is `kraj-1`.

    `RECORD_ROOT` is read here rather than bound as a default argument, which
    would fix it at import time and leave the test suite writing into the tree.
    """
    return RECORD_ROOT / out.resolve().name


def absolute_paths(value: object, key: str = "") -> list[str]:
    """The dotted key of every string under `value` that is an absolute path.

    List items are keyed by index, so a tile is `tiles.3.file`. A URL, WKT and
    a relative `dem/…tif` are not absolute paths, and pass.
    """
    if isinstance(value, dict):
        items = value.items()
    elif isinstance(value, list):
        items = enumerate(value)
    else:
        return [key] if isinstance(value, str) and os.path.isabs(value) else []
    return [
        found
        for child, item in items
        for found in absolute_paths(item, f"{key}.{child}" if key else str(child))
    ]


def write_record(manifest: dict, directory: Path, name: str) -> Path:
    """Write `manifest` minus `run` to `directory/name`, and return where.

    Refused rather than scrubbed when a machine path survives outside `run`:
    the committed bytes must not carry one, and a field added later with a path
    in it should fail loudly here rather than be caught, or missed, in review.
    """
    committed = {block: value for block, value in manifest.items() if block != "run"}
    if found := absolute_paths(committed):
        raise RecordError(
            f"{name} carries a machine path at {', '.join(found)} — re-run with --force"
        )
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / name
    write_atomically(path, (json.dumps(committed, indent=2, sort_keys=True) + "\n").encode("utf-8"))
    return path
