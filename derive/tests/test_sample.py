"""The elevation sampling stage, over a synthetic mosaic in #7's own form.

No DATABASE_URL skipif and no network, as in `test_dem.py`: the mosaic is a
handful of GeoTIFFs written here with rasterio on `dem.tile_bbox`, stitched by
`dem.build_vrt`, so no ČÚZK bytes are committed.

The terrain is a **steep plane**. Nearest-neighbour on 2 m pixels misses a
plane by decimetres; bilinear hits it exactly, so the tests can demand the
analytic height to a millimetre and tell the two apart.
"""

from __future__ import annotations

import json
from pathlib import Path

import numpy as np
import pyarrow.parquet as pq
import pytest
import rasterio
from pyproj.enums import TransformDirection
from rasterio.transform import Affine

from krpaly_derive.dem import (
    DEM_CRS,
    NODATA,
    RESOLUTION_M,
    SERVER_BLOCK_PX,
    TILE_DIR,
    build_vrt,
    tile_bbox,
    tile_name,
)
from krpaly_derive.dem import MANIFEST_NAME as DEM_MANIFEST
from krpaly_derive.dem import OUTPUT_NAME as DEM_VRT
from krpaly_derive.extract import FORWARD, REVERSE, candidate, sha256_of, write_parquet
from krpaly_derive.sample import (
    MANIFEST_NAME,
    OUTPUT_NAME,
    PINNED_OPERATION,
    SampleError,
    bilinear,
    main,
    pinned_operation,
    pinned_transformer,
    resample,
    sample,
)

# --- resampling --------------------------------------------------------------


def test_resample_keeps_both_endpoints_on_the_nodes() -> None:
    """A worked example: 5 m then 7 m, so 12 m at a 10 m step is two 6 m pieces.

    The middle sample lands 1 m into the second segment, at (4, 4); the ends
    are the first and last node exactly, which is where the anchor is.
    """
    d, xs, ys = resample(np.array([0.0, 3.0, 10.0]), np.array([0.0, 4.0, 4.0]), 10.0)
    assert d.tolist() == [0.0, 6.0, 12.0]
    assert xs.tolist() == [0.0, 4.0, 10.0]
    assert ys.tolist() == [0.0, 4.0, 4.0]


def test_resample_spacing_never_exceeds_the_step() -> None:
    d, xs, _ys = resample(np.array([0.0, 25.0]), np.array([0.0, 0.0]), 10.0)
    assert len(d) == 4
    assert np.all(np.diff(d) > 0)
    assert np.all(np.diff(d) <= 10.0)
    assert d[-1] == 25.0 and xs[-1] == 25.0


def test_resample_a_short_line_is_its_two_ends() -> None:
    d, xs, ys = resample(np.array([0.0, 0.0]), np.array([0.0, 4.0]), 10.0)
    assert d.tolist() == [0.0, 4.0]
    assert ys.tolist() == [0.0, 4.0]


# --- the mosaic, in #7's own form --------------------------------------------

# 128 px tiles, the smallest #7 allows, so the fixture is kilobytes.
TILE_PX = SERVER_BLOCK_PX
SPAN = TILE_PX * RESOLUTION_M  # 256 m

# The same block north of Ostrava `test_dem.py` uses: an L of three tiles and
# a fourth, GAP, never fetched — the hole in candidate-driven coverage.
BX, BY = -1836, -4313
BASE = (BX, BY)
EAST = (BX + 1, BY)
NORTH = (BX, BY + 1)
GAP = (BX + 1, BY + 1)
PRESENT = [BASE, EAST, NORTH]

# A real hole in coverage inside BASE, and a single pixel at exactly 0.0 inside
# EAST — what a window that lost its `noData` parameter looks like. ZERO_AT is
# on odd coordinates, which on a 2 m grid anchored at even ones is a pixel
# centre, so it names one pixel and not four.
HOLE = (-469990.0, -1104100.0, -469960.0, -1104070.0)
ZERO_AT = (-469601.0, -1104051.0)

X0, Y0 = -470000.0, -1104000.0


def plane(x, y):
    """10 % in x, 5 % in y: steep enough that nearest-neighbour misses by decimetres."""
    return 400.0 + 0.10 * (x - X0) + 0.05 * (y - Y0)


def write_tile(path: Path, tx: int, ty: int) -> None:
    xmin, _ymin, _xmax, ymax = tile_bbox(tx, ty, SPAN)
    centres_x = xmin + RESOLUTION_M * (np.arange(TILE_PX) + 0.5)
    centres_y = ymax - RESOLUTION_M * (np.arange(TILE_PX) + 0.5)
    grid_x, grid_y = np.meshgrid(centres_x, centres_y)
    band = plane(grid_x, grid_y).astype("float32")

    hx0, hy0, hx1, hy1 = HOLE
    band[(grid_x >= hx0) & (grid_x < hx1) & (grid_y >= hy0) & (grid_y < hy1)] = NODATA
    band[(np.abs(grid_x - ZERO_AT[0]) < 1) & (np.abs(grid_y - ZERO_AT[1]) < 1)] = 0.0

    profile = {
        "driver": "GTiff",
        "width": TILE_PX,
        "height": TILE_PX,
        "count": 1,
        "dtype": "float32",
        "crs": f"EPSG:{DEM_CRS}",
        "transform": Affine(RESOLUTION_M, 0.0, xmin, 0.0, -RESOLUTION_M, ymax),
        "tiled": True,
        "blockxsize": SERVER_BLOCK_PX,
        "blockysize": SERVER_BLOCK_PX,
        "nodata": NODATA,
    }
    with rasterio.open(path, "w", **profile) as raster:
        raster.write(band, 1)


def write_mosaic(out: Path, complete: bool = True) -> None:
    """Tiles, `dem.build_vrt` over them, and the least of a manifest #8 reads."""
    (out / TILE_DIR).mkdir(parents=True, exist_ok=True)
    entries = []
    for tx, ty in PRESENT:
        name = f"{TILE_DIR}/{tile_name(tx, ty)}"
        write_tile(out / name, tx, ty)
        entries.append({"tx": tx, "ty": ty, "file": name})
    build_vrt(entries, TILE_PX, out / DEM_VRT)
    manifest = {
        "grid": {"tile_px": TILE_PX, "tile_m": SPAN},
        "output": {"vrt": DEM_VRT, "sha256": sha256_of(out / DEM_VRT)},
        "run": {"complete": complete},
    }
    (out / DEM_MANIFEST).write_text(json.dumps(manifest, indent=2, sort_keys=True))


@pytest.fixture(scope="module")
def mosaic(tmp_path_factory) -> Path:
    out = tmp_path_factory.mktemp("mosaic") / "kraj-1"
    write_mosaic(out)
    return out


def read_at(out: Path, points: list[tuple[float, float]]) -> np.ndarray:
    xs = np.array([x for x, _y in points])
    ys = np.array([y for _x, y in points])
    with rasterio.open(out / DEM_VRT) as src:
        return bilinear(src, xs, ys, SPAN)


# --- bilinear ----------------------------------------------------------------


def test_bilinear_is_exact_on_a_plane(mosaic: Path) -> None:
    """Off-centre probes hit the analytic plane to a millimetre.

    Each probe sits far enough from its pixel's centre that nearest-neighbour
    would miss by more than a centimetre — asserted, so the test cannot pass
    against the interpolation it exists to rule out.
    """
    points = [(-469900.3, -1104010.7), (-469841.9, -1103950.2), (-469990.5, -1103700.25)]
    got = read_at(mosaic, points)
    for (x, y), value in zip(points, got, strict=True):
        assert abs(value - plane(x, y)) < 1e-3
        centre = (np.floor(x / 2) * 2 + 1, np.ceil(y / 2) * 2 - 1)
        assert abs(plane(*centre) - plane(x, y)) > 1e-2


def test_bilinear_is_continuous_across_tile_seams(mosaic: Path) -> None:
    """Across BASE|EAST in x and BASE|NORTH in y, in quarter-metre steps.

    Within a pixel of a seam the four neighbours straddle two tiles, so this is
    what fails if each tile's window is read without its 1 px pad.
    """
    offsets = np.arange(-3.0, 3.01, 0.25)
    seam_x = float(EAST[0] * SPAN)
    seam_y = float(NORTH[1] * SPAN)
    across_x = [(seam_x + dx, -1103950.3) for dx in offsets]
    across_y = [(-469900.3, seam_y + dy) for dy in offsets]
    for points in (across_x, across_y):
        got = read_at(mosaic, points)
        want = np.array([plane(x, y) for x, y in points])
        assert np.max(np.abs(got - want)) < 1e-3


def test_bilinear_reads_nodata_and_zero_as_nan(mosaic: Path) -> None:
    """Any of the four neighbours on nodata, or on exactly 0.0, is NaN.

    The zero pixel is weighted 1 by a probe on its centre and would still be
    averaged in by one a metre off it; either way it is not terrain.
    """
    points = {
        "in the hole": (-469975.0, -1104085.0),
        "a neighbour in the hole": (-469959.5, -1104085.0),
        "clear of the hole": (-469955.0, -1104085.0),
        "in the gap tile": (GAP[0] * SPAN + 100.0, GAP[1] * SPAN + 100.0),
        "on the zero": ZERO_AT,
        "a neighbour on the zero": (ZERO_AT[0] + 1.0, ZERO_AT[1] - 0.5),
        "clear of the zero": (ZERO_AT[0] + 4.0, ZERO_AT[1]),
    }
    got = dict(zip(points, read_at(mosaic, list(points.values())), strict=True))
    nan = {name for name, value in got.items() if np.isnan(value)}
    assert nan == {
        "in the hole",
        "a neighbour in the hole",
        "in the gap tile",
        "on the zero",
        "a neighbour on the zero",
    }
    for name in ("clear of the hole", "clear of the zero"):
        assert abs(got[name] - plane(*points[name])) < 1e-3


# --- the stage ---------------------------------------------------------------

STEP_M = 10.0

# Lines in EPSG:5514, each written in both directions. Named for what each is
# there to prove; the counts below follow from the names.
LINES_M = {
    # Three vertices, off-centre everywhere, clear of the hole.
    "plane": [(-469900.3, -1104010.7), (-469841.9, -1103950.2), (-469830.0, -1103900.0)],
    # BASE into EAST, across the seam.
    "seam": [(-469800.0, -1103950.0), (-469720.0, -1103940.0)],
    # 4 m: shorter than a step, so exactly its two ends.
    "short": [(-469880.0, -1103990.0), (-469876.0, -1103990.0)],
    # Starts on terrain and runs into the hole.
    "hole": [(-470000.0, -1104085.0), (-469940.0, -1104085.0)],
    # Wholly inside the tile #7 never fetched.
    "gap": [(-469700.0, -1103800.0), (-469650.0, -1103750.0)],
    # Starts on the 0.0 pixel. An endpoint rather than a middle sample: the
    # round trip through #6's lon/lat makes a 40 m line 40.0000001 m, which
    # is five 8 m steps rather than four 10 m ones, and a middle sample would
    # step over the pixel. The ends are always the nodes.
    "zero": [ZERO_AT, (ZERO_AT[0] + 40.0, ZERO_AT[1])],
    # Two distinct nodes on one spot: no length to profile.
    "degenerate": [(-469870.0, -1103980.0), (-469870.0, -1103980.0)],
}
KEPT = ("plane", "seam", "short")


def to_degrees(points: list[tuple[float, float]]) -> list[tuple[float, float]]:
    """Through the pin itself, so a line's 5514 length survives into #6's lon/lat."""
    pinned = pinned_transformer()
    return [pinned.transform(x, y, direction=TransformDirection.INVERSE) for x, y in points]


def write_candidates(path: Path) -> dict[str, tuple[int, int]]:
    """`candidates.parquet` as #6 writes it; returns each line's (forward, reverse) ids."""
    rows, ids = [], {}
    for index, (name, line) in enumerate(LINES_M.items()):
        coords = to_degrees(line)
        node_ids = [10 * index + 1, 10 * index + 2]
        ids[name] = (len(rows), len(rows) + 1)
        rows.append(candidate(index, node_ids, coords, FORWARD))
        rows.append(candidate(index, node_ids[::-1], coords[::-1], REVERSE))
    write_parquet(rows, path)
    return ids


def run(out: Path, **extra) -> int:
    options = {
        "out": out,
        "candidates": out / "candidates.parquet",
        "dem": out / DEM_VRT,
        "step_m": STEP_M,
        "force": False,
    }
    return sample(**{**options, **extra})


def profiles_of(out: Path) -> dict[int, dict]:
    rows = pq.read_table(out / OUTPUT_NAME).to_pylist()
    return {row["candidate_id"]: row for row in rows}


def manifest_of(out: Path) -> dict:
    return json.loads((out / MANIFEST_NAME).read_text())


@pytest.fixture
def staged(tmp_path: Path) -> Path:
    out = tmp_path / "kraj-1"
    write_mosaic(out)
    write_candidates(out / "candidates.parquet")
    return out


@pytest.fixture(scope="module")
def sampled(tmp_path_factory) -> tuple[Path, dict[str, tuple[int, int]]]:
    """One complete run, read by the tests that do not mutate it."""
    out = tmp_path_factory.mktemp("sampled") / "kraj-1"
    write_mosaic(out)
    ids = write_candidates(out / "candidates.parquet")
    assert run(out) == 0
    return out, ids


def test_nodata_drops_the_whole_candidate_and_is_counted(sampled) -> None:
    """The issue's own failure: a hole never becomes a sea-level climb.

    Dropped whole rather than trimmed, because trimming would detach the
    geometry from its start and end nodes — the anchor.
    """
    out, ids = sampled
    profiles = profiles_of(out)
    assert sorted(profiles) == sorted(i for name in KEPT for i in ids[name])

    counts = manifest_of(out)["counts"]
    assert counts["candidates"] == 2 * len(LINES_M)
    assert counts["kept"] == 2 * len(KEPT)
    assert counts["degenerate"] == 2
    assert counts["nodata_whole"] == 2  # the gap, both ways
    assert counts["nodata_partial"] == 4  # the hole and the zero, both ways
    assert counts["nodata_samples"] > 0
    assert counts["samples"] == sum(row["n_samples"] for row in profiles.values())

    for row in profiles.values():
        assert all(value > 0 and value != NODATA for value in row["elevation_m"])


def test_profiles_are_engine_tuples_on_the_plane(sampled) -> None:
    """Distance from 0 to the line's own length in ≤10 m steps, terrain under each."""
    out, ids = sampled
    profiles = profiles_of(out)
    pinned = pinned_transformer()
    for name in KEPT:
        line = LINES_M[name]
        length = sum(
            np.hypot(x1 - x0, y1 - y0) for (x0, y0), (x1, y1) in zip(line, line[1:], strict=False)
        )
        row = profiles[ids[name][0]]
        d = np.array(row["distance_m"])
        assert row["n_samples"] == len(d) == len(row["elevation_m"]) == len(row["lat"])
        assert d[0] == 0.0
        assert np.all(np.diff(d) > 0) and np.all(np.diff(d) <= STEP_M)
        # Not to the bit: the line went through #6's lon/lat and back, and
        # the pin round-trips to ~1 mm at each end.
        assert abs(d[-1] - length) < 5e-3

        # The ends are the OSM nodes, which is where the anchor is.
        (first_lon, first_lat), (last_lon, last_lat) = to_degrees([line[0], line[-1]])
        assert abs(row["lon"][0] - first_lon) < 1e-7 and abs(row["lat"][0] - first_lat) < 1e-7
        assert abs(row["lon"][-1] - last_lon) < 1e-7 and abs(row["lat"][-1] - last_lat) < 1e-7

        xs, ys = pinned.transform(np.array(row["lon"]), np.array(row["lat"]))
        assert np.max(np.abs(np.array(row["elevation_m"]) - plane(xs, ys))) < 1e-3

    assert profiles[ids["short"][0]]["n_samples"] == 2


def test_rows_keep_the_order_of_the_candidates(sampled) -> None:
    """Row order is `candidates.parquet`'s, which is part of "same inputs, same bytes"."""
    out, _ids = sampled
    written = pq.read_table(out / OUTPUT_NAME).column("candidate_id").to_pylist()
    # plane, seam and short are the first three lines, both ways each.
    assert written == [0, 1, 2, 3, 4, 5]


def test_refuses_a_proj_without_the_pinned_operation(monkeypatch) -> None:
    """A PROJ that lacks EPSG:5239 is refused, not quietly handed its own per-point choice."""
    pinned_operation.cache_clear()
    pinned_transformer.cache_clear()
    monkeypatch.setattr("krpaly_derive.sample.PINNED_OPERATION", 999999)
    try:
        with pytest.raises(SampleError) as raised:
            pinned_transformer()
        assert "EPSG:999999" in str(raised.value)
    finally:
        pinned_operation.cache_clear()
        pinned_transformer.cache_clear()


def test_both_directions_are_one_profile_mirrored(sampled) -> None:
    out, ids = sampled
    profiles = profiles_of(out)
    for name in KEPT:
        forward, reverse = (profiles[i] for i in ids[name])
        back = reverse["elevation_m"][::-1]
        # The same vertex through the same pin and the same read: bit-equal.
        assert back[0] == forward["elevation_m"][0]
        assert back[-1] == forward["elevation_m"][-1]
        assert np.max(np.abs(np.array(back) - np.array(forward["elevation_m"]))) < 1e-3


def test_manifest_records_the_pin_and_both_inputs(sampled) -> None:
    out, _ids = sampled
    written = manifest_of(out)
    assert written["transform"]["code"] == PINNED_OPERATION
    assert written["source"]["sha256"] == sha256_of(out / "candidates.parquet")
    assert written["source"]["dem_sha256"] == sha256_of(out / DEM_VRT)
    assert written["sampling"]["step_m"] == STEP_M
    assert written["sampling"]["method"] == "bilinear"
    assert written["output"]["sha256"] == sha256_of(out / OUTPUT_NAME)


def test_refuses_an_incomplete_mosaic(tmp_path: Path) -> None:
    out = tmp_path / "kraj-1"
    write_mosaic(out, complete=False)
    write_candidates(out / "candidates.parquet")
    with pytest.raises(SampleError) as raised:
        run(out)
    assert "complete" in str(raised.value)
    assert not (out / OUTPUT_NAME).exists()


def test_rerun_is_skipped_and_the_output_deterministic(staged: Path, capsys) -> None:
    assert run(staged) == 0
    first = sha256_of(staged / OUTPUT_NAME)
    written_at = (staged / OUTPUT_NAME).stat().st_mtime_ns
    capsys.readouterr()

    assert run(staged) == 0
    assert "already sampled" in capsys.readouterr().err
    assert (staged / OUTPUT_NAME).stat().st_mtime_ns == written_at

    # --force re-samples, and the same inputs give the same bytes.
    assert run(staged, force=True) == 0
    assert (staged / OUTPUT_NAME).stat().st_mtime_ns != written_at
    assert sha256_of(staged / OUTPUT_NAME) == first


def test_main_runs_on_its_defaults(staged: Path) -> None:
    assert main(["--out", str(staged)]) == 0
    assert manifest_of(staged)["counts"]["kept"] == 2 * len(KEPT)


def test_main_says_what_is_missing(tmp_path: Path) -> None:
    with pytest.raises(SystemExit) as raised:
        main(["--out", str(tmp_path / "nowhere")])
    assert str(raised.value).startswith("sample: ")


# --- the transform -----------------------------------------------------------


def test_pinned_transformer_round_trips() -> None:
    """One operation both ways: forward then inverse is identity to ~1 cm.

    PROJ's inverse Helmert is approximate — ~1e-8° measured — which is a
    millimetre; 1e-7° is a centimetre and far below anything a climb notices.
    Includes a point east of 18.86 E, past the operation's area of use, which
    the 10 km buffer reaches.
    """
    pinned = pinned_transformer()
    for lon, lat in [(18.5, 49.55), (18.2, 49.8), (17.5, 50.3), (19.2, 49.5)]:
        x, y = pinned.transform(lon, lat)
        back_lon, back_lat = pinned.transform(x, y, direction=TransformDirection.INVERSE)
        assert abs(back_lon - lon) < 1e-7
        assert abs(back_lat - lat) < 1e-7


def test_pinned_transformer_is_epsg_5239() -> None:
    """The Helmert EPSG publishes for "S-JTSK to WGS 84 (5)", and no other.

    Read off the pipeline rather than the description: a pipeline built with
    `from_pipeline` describes itself as its own PROJ string. The translation
    is EPSG:5239's published one; (1), the operation a substring match on
    "5239" also finds, has a different one.
    """
    assert PINNED_OPERATION == 5239
    definition = pinned_transformer().definition
    assert "helmert" in definition
    assert "x=572.213" in definition and "y=85.334" in definition and "z=461.94" in definition
