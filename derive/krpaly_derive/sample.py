"""Stage 3: candidate polylines and the DMR 5G mosaic in, engine-shaped profiles out.

    uv run --directory derive python -m krpaly_derive.sample --out data/kraj-1

Joins #6's `candidates.parquet` to #7's `dem.vrt`. Every candidate, in both
directions, is resampled to equal steps of at most 10 m along its own length
and the terrain read under each step bilinearly, so what comes out is
`[distance_m, elevation_m, lat, lon]` — `detectClimbs`' argument verbatim, with
distance taken as given rather than recomputed by the engine (#9).

Three properties are the reason this stage exists as its own file on disk:

* **Nodata is reported, never absorbed.** A candidate any of whose samples
  touches nodata — or a pixel at exactly 0.0, which is never a Czech elevation
  (derive/INPUTS.md § Nodata, measured) — is dropped whole and counted by
  reason. Trimming it would detach the geometry from `start_node_id` and
  `end_node_id`, which are the anchor; interpolating across a border would
  invent terrain.
* **One transform, pinned.** `Transformer.from_crs` picks its operation *per
  point*: at 18.5 E 49.55 N it uses Slovakia's (4), whose area ends at 49.61 N
  — inside Moravskoslezský — and a Czech one north of it. The candidates
  differ by up to ~2.5 m, so an unpinned transformer puts a metre-scale step
  through the middle of the kraj. Both directions go through EPSG:5239 alone.
* **Bilinear, not nearest.** Nearest-neighbour on 2 m pixels is a staircase
  the detector reads as grade noise; on a plane bilinear is exact.

**Bridges and tunnels are sampled as terrain.** DMR 5G is bare earth, so a
viaduct samples the valley under it and a tunnel the hill over it — a
fictional climb either way. That is #22, and it must land before #11 loads
anything.

It does not run the engine (#9), chain or dedupe candidates (#10), or load
anything (#11).
"""

from __future__ import annotations

import argparse
import functools
import json
import math
import os
import sys
import time
from datetime import UTC, datetime
from pathlib import Path

import numpy as np
import pyarrow as pa
import pyarrow.parquet as pq
import pyproj
import rasterio
import shapely
from pyproj import Transformer
from pyproj.enums import TransformDirection
from pyproj.transformer import TransformerGroup
from rasterio.io import DatasetReader
from rasterio.windows import Window

# The mosaic's names and its nodata value are read from the module that writes
# them, as dem.py reads #6's, so the two stages cannot drift apart.
from krpaly_derive.dem import MANIFEST_NAME as DEM_MANIFEST
from krpaly_derive.dem import (
    NODATA,
    PROJECTED_CRS,
    SOURCE_CRS,
    read_manifest,
    tile_indices,
    write_atomically,
)
from krpaly_derive.dem import OUTPUT_NAME as DEM_NAME
from krpaly_derive.extract import ROW_GROUP_SIZE, already_done, sha256_of

SOURCE_NAME = "candidates.parquet"
OUTPUT_NAME = "profiles.parquet"
MANIFEST_NAME = "profiles.manifest.json"

# ≤10 m is #8's figure: fine enough that a 2 m-pixel road reads as a road
# rather than as its pixels, coarse enough that a kraj is a few million samples.
DEFAULT_STEP_M = 10.0

# "S-JTSK to WGS 84 (5)": Czechia, 1 m, a seven-parameter Helmert with no grid,
# so it gives the same answer on every machine. Its area of use reaches
# 18.86 E, Czechia's eastern tip; a Helmert has no edge, so the buffer past it
# is transformed by the same formula rather than by nothing.
PINNED_OPERATION = 5239

METHOD = "bilinear"
NODATA_RULE = (
    "a candidate any of whose samples has a nodata or 0.0 pixel among its four "
    "neighbours is dropped whole"
)

# GDAL keeps this many sources open behind a VRT. The default of 100 is far
# below a kraj's tile count, and tile-major reads revisit a column of
# neighbours every few hundred tiles — each re-open costs a header parse.
POOL_HEADROOM = 64

# Parallel lists rather than a list of 4-tuples: columnar, and each dtype is
# honest about its own precision.
SCHEMA = pa.schema(
    [
        # The join key into candidates.parquet. Only candidates that were kept
        # have a row; an anti-join on this names the ones that were not.
        ("candidate_id", pa.uint64()),
        ("n_samples", pa.int32()),
        ("distance_m", pa.list_(pa.float64())),
        # float32: the mosaic's own precision, and no more is known.
        ("elevation_m", pa.list_(pa.float32())),
        ("lat", pa.list_(pa.float64())),
        ("lon", pa.list_(pa.float64())),
    ]
)


class SampleError(Exception):
    """An input this stage cannot sample, said in one line."""


def uses_operation(candidate: Transformer, code: int) -> bool:
    """Whether one of an operation's steps *is* EPSG:`code`, by its id.

    The code alone, because the authority reads `INVERSE(DERIVED_FROM(EPSG))`:
    the group runs the published S-JTSK → WGS84 operation backwards. Never a
    substring of the JSON — "5239" also appears inside (1), EPSG:1623.
    """
    return any(
        (step.to_json_dict().get("id") or {}).get("code") == code
        for step in candidate.operations or ()
    )


@functools.cache
def pinned_operation() -> Transformer:
    """The one WGS84 → S-JTSK operation this stage uses, as PROJ lists it.

    Kept apart from `pinned_transformer` because only this object can say what
    it is — a pipeline built from its definition describes itself as the PROJ
    string — and the manifest records its name and accuracy.
    """
    group = TransformerGroup(SOURCE_CRS, PROJECTED_CRS, always_xy=True)
    for candidate in group.transformers:
        if uses_operation(candidate, PINNED_OPERATION):
            return candidate
    raise SampleError(
        f"EPSG:{PINNED_OPERATION} is not among the {len(group.transformers)} WGS84 → S-JTSK "
        f"operations PROJ {pyproj.proj_version_str} offers here — the pin cannot be honoured"
    )


@functools.cache
def pinned_transformer() -> Transformer:
    """WGS84 → EPSG:5514 through EPSG:5239 alone, for every point and both ways.

    `from_pipeline` is what stops PROJ choosing per point. The way back is the
    same object with `direction=INVERSE`, so both directions are one operation;
    they round-trip to ~1e-8° (a millimetre — PROJ's inverse Helmert is
    approximate), not to the bit.
    """
    return Transformer.from_pipeline(pinned_operation().definition)


def resample(
    x: np.ndarray, y: np.ndarray, step: float
) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """One line, in metres, at equal steps of at most `step` along its length.

    `L/n ≤ step` rather than `step` exactly, so both endpoints land on the
    first and last node — the anchor — instead of the last sample falling
    short of it. Distance is arc length along the polyline in Krovák metres,
    whose scale error of ~1e-4 is a millimetre in 10 m.
    """
    s = np.concatenate(([0.0], np.cumsum(np.hypot(np.diff(x), np.diff(y)))))
    length = float(s[-1])
    n = max(1, math.ceil(length / step))
    d = np.linspace(0.0, length, n + 1)
    return d, np.interp(d, s, x), np.interp(d, s, y)


def bilinear(src: DatasetReader, xs: np.ndarray, ys: np.ndarray, tile_m: int) -> np.ndarray:
    """Terrain under every point, bilinear on pixel centres; NaN where it is not terrain.

    **Tile-major**, not per candidate: the points are grouped by #7's tile grid
    and each tile is read once, as one window padded by a pixel on every side
    so a point within a pixel of a seam still has all four neighbours. That is
    a couple of thousand window reads for a kraj rather than one per candidate
    — and the pad is filled with nodata where the neighbour was never fetched.

    A point is NaN if any of its four neighbours is nodata *or exactly 0.0*,
    which is never a Czech elevation. Weighting alone would not do: a
    neighbour at −9999 with a weight of 0.01 is still a hundred-metre cliff.
    """
    out = np.full(len(xs), np.nan)
    if len(xs) == 0:
        return out

    res = src.res[0]
    left, top = src.transform.c, src.transform.f
    if left % tile_m or top % tile_m or src.res != (res, res):
        raise SampleError(
            f"the mosaic's origin ({left}, {top}) or its {src.res} m pixels are not on "
            f"#7's {tile_m} m tile grid — it was not built by krpaly_derive.dem"
        )
    px = round(tile_m / res)

    tiles, inverse = np.unique(
        np.stack([tile_indices(xs, tile_m), tile_indices(ys, tile_m)], axis=1),
        axis=0,
        return_inverse=True,
    )
    order = np.argsort(inverse, kind="stable")
    bounds = np.searchsorted(inverse[order], np.arange(len(tiles) + 1))

    for k, (tx, ty) in enumerate(tiles.tolist()):
        idx = order[bounds[k] : bounds[k + 1]]
        x0, y1 = tx * tile_m, (ty + 1) * tile_m
        block = read_padded(src, round((x0 - left) / res) - 1, round((top - y1) / res) - 1, px + 2)

        # Fractional pixel coordinates in the padded block, measured between
        # centres: column i's centre is x0 + (i + 0.5)·res, and the pad adds 1.
        col = (xs[idx] - x0) / res + 0.5
        row = (y1 - ys[idx]) / res + 0.5
        c0 = np.floor(col).astype(np.int64)
        r0 = np.floor(row).astype(np.int64)
        fc, fr = col - c0, row - r0

        corners = np.stack(
            [block[r0, c0], block[r0, c0 + 1], block[r0 + 1, c0], block[r0 + 1, c0 + 1]]
        )
        weights = np.stack([(1 - fc) * (1 - fr), fc * (1 - fr), (1 - fc) * fr, fc * fr])
        values = (corners * weights).sum(axis=0)
        values[((corners == NODATA) | (corners == 0.0)).any(axis=0)] = np.nan
        out[idx] = values
    return out


def read_padded(src: DatasetReader, col_off: int, row_off: int, size: int) -> np.ndarray:
    """A square window that may overhang the mosaic, the overhang read as nodata.

    Clipped and placed by hand rather than read with `boundless=True`, which
    wraps the dataset in a fresh VRT on every call: 53 ms a window against
    6 ms on kraj-1's mosaic, measured. Inside the mosaic an unfetched tile
    already reads as nodata — that is the VRT's `<NoDataValue>` — so the
    overhang is the only part filled here.
    """
    block = np.full((size, size), NODATA, dtype=np.float64)
    c0, r0 = max(col_off, 0), max(row_off, 0)
    c1, r1 = min(col_off + size, src.width), min(row_off + size, src.height)
    if c1 > c0 and r1 > r0:
        block[r0 - row_off : r1 - row_off, c0 - col_off : c1 - col_off] = src.read(
            1, window=Window(c0, r0, c1 - c0, r1 - r0)
        )
    return block


def list_column(offsets: np.ndarray, values: np.ndarray, kind: pa.DataType) -> pa.ListArray:
    """A ragged column from one flat array, without a Python list per row."""
    return pa.ListArray.from_arrays(pa.array(offsets, pa.int32()), pa.array(values, kind))


def write_parquet(table: pa.Table, path: Path) -> None:
    """Atomically and in a fixed shape, as #6 does, so the same inputs give the same bytes."""
    tmp = path.with_suffix(path.suffix + ".tmp")
    pq.write_table(table, tmp, compression="zstd", row_group_size=ROW_GROUP_SIZE, version="2.6")
    os.replace(tmp, path)


def stage_signature(
    source_sha256: str, dem_sha256: str, step_m: float
) -> dict[tuple[str, str], object]:
    """Everything about a run that changes what comes out of it, keyed as #6 keys it."""
    return {
        ("source", "sha256"): source_sha256,
        ("source", "dem_sha256"): dem_sha256,
        ("sampling", "step_m"): step_m,
        ("sampling", "method"): METHOD,
        ("transform", "code"): PINNED_OPERATION,
    }


def read_mosaic_manifest(dem: Path) -> dict:
    """#7's manifest, refused unless it says the mosaic is finished.

    An incomplete mosaic reads its missing half as legitimate nodata, so every
    candidate there would be dropped and counted as if ČÚZK had no coverage —
    a plausible number, and a wrong one.
    """
    path = dem.parent / DEM_MANIFEST
    manifest = read_manifest(path)
    if not manifest.get("run", {}).get("complete"):
        raise SampleError(
            f"{path} does not say the mosaic is complete — finish krpaly_derive.dem first"
        )
    if not dem.is_file():
        raise SampleError(f"{dem} is not a file, though {path} says the mosaic is complete")
    recorded = manifest.get("output", {}).get("sha256")
    if recorded != sha256_of(dem):
        raise SampleError(f"{dem} is not the mosaic {path} records — re-run krpaly_derive.dem")
    return manifest


def report(written: dict) -> None:
    """What the run kept and why it dropped the rest, on stderr, as dem.py reports."""
    counts = written["counts"]
    print(
        f"candidates: {counts['kept']} of {counts['candidates']} kept, "
        f"{counts['samples']} samples at ≤{written['sampling']['step_m']:g} m",
        file=sys.stderr,
    )
    print(
        f"nodata: {counts['nodata_whole']} wholly and {counts['nodata_partial']} partly "
        f"uncovered, dropped — {counts['nodata_samples']} samples",
        file=sys.stderr,
    )
    if counts["degenerate"]:
        print(f"degenerate: {counts['degenerate']} with no length, dropped", file=sys.stderr)
    print(
        f"profiles: {written['output']['file']}, {written['run']['wall_clock_s']} s",
        file=sys.stderr,
    )


def sample(out: Path, candidates: Path, dem: Path, step_m: float, force: bool) -> int:
    if not candidates.is_file():
        raise SampleError(
            f"{candidates} is not a file — run krpaly_derive.extract first, or pass --candidates"
        )
    if step_m <= 0:
        raise SampleError(f"--step-m {step_m} is not a distance")
    mosaic = read_mosaic_manifest(dem)

    out.mkdir(parents=True, exist_ok=True)
    output_path = out / OUTPUT_NAME
    manifest_path = out / MANIFEST_NAME

    started = time.monotonic()
    started_at = datetime.now(UTC).isoformat(timespec="seconds")

    source_sha256 = sha256_of(candidates)
    dem_sha256 = mosaic["output"]["sha256"]
    signature = stage_signature(source_sha256, dem_sha256, step_m)
    if not force and already_done(manifest_path, output_path, signature):
        print(
            f"{output_path} is already sampled from these inputs — pass --force to redo",
            file=sys.stderr,
        )
        return 0

    # Every vertex of every candidate, both directions, in one vectorised call.
    # No row pairing is assumed: each direction is sampled on its own, which
    # costs a few million bilinear reads and nothing else.
    table = pq.read_table(candidates, columns=["candidate_id", "geometry"])
    ids = table.column("candidate_id").to_numpy()
    lines = shapely.from_wkb(table.column("geometry").to_numpy(zero_copy_only=False))
    coords = shapely.get_coordinates(lines)
    starts = np.concatenate(([0], np.cumsum(shapely.get_num_coordinates(lines))))
    pinned = pinned_transformer()
    x, y = pinned.transform(coords[:, 0], coords[:, 1])

    n_samples = np.zeros(len(ids), dtype=np.int64)
    parts: tuple[list, list, list] = ([], [], [])
    for i in range(len(ids)):
        d, xs, ys = resample(x[starts[i] : starts[i + 1]], y[starts[i] : starts[i + 1]], step_m)
        # Distinct nodes on one spot: #6 keeps them, because the fix is
        # upstream, but there is no profile to take.
        if d[-1] == 0.0:
            continue
        n_samples[i] = len(d)
        for part, values in zip(parts, (d, xs, ys), strict=True):
            part.append(values)
    d, xs, ys = (np.concatenate(part) if part else np.empty(0) for part in parts)

    pool = len(mosaic.get("tiles", [])) + POOL_HEADROOM
    with rasterio.Env(GDAL_MAX_DATASET_POOL_SIZE=pool), rasterio.open(dem) as src:
        z = bilinear(src, xs, ys, mosaic["grid"]["tile_m"])

    # Per-candidate NaN counts by prefix sum: `np.add.reduceat` would misread
    # the zero-length segments a degenerate candidate leaves.
    offsets = np.concatenate(([0], np.cumsum(n_samples)))
    nan_prefix = np.concatenate(([0], np.cumsum(np.isnan(z))))
    nan_per = nan_prefix[offsets[1:]] - nan_prefix[offsets[:-1]]
    sampled = n_samples > 0
    whole = sampled & (nan_per == n_samples)
    partial = sampled & (nan_per > 0) & ~whole
    kept = sampled & (nan_per == 0)

    keep = np.repeat(kept, n_samples)
    lon, lat = pinned.transform(xs[keep], ys[keep], direction=TransformDirection.INVERSE)
    kept_offsets = np.concatenate(([0], np.cumsum(n_samples[kept])))
    profiles = pa.table(
        {
            "candidate_id": pa.array(ids[kept], pa.uint64()),
            "n_samples": pa.array(n_samples[kept], pa.int32()),
            "distance_m": list_column(kept_offsets, d[keep], pa.float64()),
            "elevation_m": list_column(kept_offsets, z[keep].astype(np.float32), pa.float32()),
            "lat": list_column(kept_offsets, lat, pa.float64()),
            "lon": list_column(kept_offsets, lon, pa.float64()),
        },
        schema=SCHEMA,
    )
    write_parquet(profiles, output_path)

    operation = pinned_operation()
    manifest = {
        "source": {
            "file": candidates.name,
            "sha256": source_sha256,
            "bytes": candidates.stat().st_size,
            "dem": dem.name,
            "dem_sha256": dem_sha256,
        },
        "sampling": {"step_m": step_m, "method": METHOD, "nodata_rule": NODATA_RULE},
        "transform": {
            "code": PINNED_OPERATION,
            "operation": operation.description,
            "accuracy_m": operation.accuracy,
            "proj_version": pyproj.proj_version_str,
            # What #7 planned its tiles with, which is PROJ's own per-point
            # choice rather than the pin: its 32 m halo absorbs the difference,
            # and recording both makes the difference visible.
            "dem_planned_with": mosaic.get("grid", {}).get("transform"),
        },
        "counts": {
            "candidates": len(ids),
            "kept": int(kept.sum()),
            "degenerate": int((~sampled).sum()),
            "nodata_whole": int(whole.sum()),
            "nodata_partial": int(partial.sum()),
            "nodata_samples": int(np.isnan(z).sum()),
            "samples": int(n_samples[kept].sum()),
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
    report(manifest)
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="krpaly_derive.sample", description=__doc__)
    parser.add_argument(
        "--out", required=True, type=Path, help="the stage directory #6 and #7 wrote"
    )
    parser.add_argument(
        "--candidates",
        type=Path,
        default=None,
        help=f"candidate polylines to sample (default: <out>/{SOURCE_NAME})",
    )
    parser.add_argument(
        "--dem",
        type=Path,
        default=None,
        help=f"the mosaic, beside its manifest (default: <out>/{DEM_NAME})",
    )
    parser.add_argument(
        "--step-m",
        type=float,
        default=DEFAULT_STEP_M,
        help=f"largest distance between samples (default: {DEFAULT_STEP_M:g})",
    )
    parser.add_argument(
        "--force", action="store_true", help="re-sample even if the output is already current"
    )
    args = parser.parse_args(argv)

    try:
        return sample(
            out=args.out,
            candidates=args.candidates or args.out / SOURCE_NAME,
            dem=args.dem or args.out / DEM_NAME,
            step_m=args.step_m,
            force=args.force,
        )
    except SampleError as error:
        raise SystemExit(f"sample: {error}") from error


if __name__ == "__main__":
    raise SystemExit(main())
