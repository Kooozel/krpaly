"""The DEM acquisition stage, over a synthetic service.

No DATABASE_URL skipif: none of this needs Postgres. **And no network** — the
stage's only socket lives in `fetch_window`, which is a parameter of `fetch`
defaulting to the real one, so every test here injects a stub instead. Nothing
outside `tmp_path` is read or written, and no ČÚZK bytes are committed: a
synthetic raster carries none of the CC BY 4.0 obligation that
`CONTRIBUTING.md` § "Test data" would attach to a real terrain fixture.

The candidates file is built here with #6's own `candidate` and `write_parquet`
rather than read from its fixture, which both proves the two stages chain and
lets the tile set be chosen. It is chosen **L-shaped**: three tiles of a two by
two block, so the mosaic has a hole in it that no run will ever fill. That hole
is what `test_vrt_gap_reads_nodata` reads, and it is the failure this whole
stage exists to prevent.
"""

from __future__ import annotations

import json
import math
from pathlib import Path

import numpy as np
import pytest
import rasterio
from pyproj import Transformer
from rasterio.crs import CRS
from rasterio.transform import Affine

from krpaly_derive.dem import (
    DEM_CRS,
    MANIFEST_NAME,
    NODATA,
    OUTPUT_NAME,
    RESOLUTION_M,
    SERVER_BLOCK_PX,
    TILE_DIR,
    DemError,
    declared_epsg,
    fetch,
    fetch_window,
    main,
    plan_tiles,
    tile_bbox,
    tile_index,
    tile_indices,
    tile_name,
    tile_span,
    window_url,
)
from krpaly_derive.extract import FORWARD, REVERSE, candidate, write_parquet

# 128 px windows — the smallest the service's own block size allows, so the
# rasters a test writes are 64 KB rather than 4 MB.
TILE_PX = SERVER_BLOCK_PX
SPAN = TILE_PX * RESOLUTION_M  # 256 m
HALO_M = 8.0

# A block of ground north of Ostrava, in EPSG:5514. Every index below is
# negative, which is the point: it is where `int()` and `math.floor()` differ.
BX, BY = -1836, -4313

BASE = (BX, BY)
EAST = (BX + 1, BY)
NORTH = (BX, BY + 1)
GAP = (BX + 1, BY + 1)  # never planned, never fetched — the hole
PLANNED = sorted([BASE, EAST, NORTH])

# Two candidates, an L: one running east out of BASE into EAST, one running
# north out of BASE into NORTH. Placed well clear of every tile edge so the
# halo cannot reach a fourth tile.
LINES_M = [
    [(-469900.0, -1104000.0), (-469700.0, -1104000.0)],
    [(-469900.0, -1104000.0), (-469900.0, -1103800.0)],
]

# A rectangle of nodata inside BASE — a real hole in ČÚZK's coverage — and a
# single 0.0 pixel inside EAST, which is what a window that quietly lost its
# `noData` parameter looks like. NORTH is wholly uncovered, as a window over
# Poland is.
HOLE = (-469850.0, -1104100.0, -469820.0, -1104070.0)
ZERO_AT = (-469700.0, -1104000.0)


def to_degrees(points: list[tuple[float, float]]) -> list[tuple[float, float]]:
    """EPSG:5514 → WGS84, because #6 writes lon/lat and this file thinks in metres."""
    transformer = Transformer.from_crs(f"EPSG:{DEM_CRS}", "OGC:CRS84", always_xy=True)
    return [transformer.transform(x, y) for x, y in points]


def snap_x(value: float) -> float:
    """The centre of the 2 m column holding a coordinate.

    Every tile origin is a multiple of the tile span and therefore of 2, so one
    global lattice serves every tile.
    """
    return math.floor(value / RESOLUTION_M) * RESOLUTION_M + RESOLUTION_M / 2


def snap_y(value: float) -> float:
    """The centre of the 2 m row holding a coordinate.

    Not `snap_x` with a different argument: rows are numbered downward from
    `ymax`, so a coordinate landing exactly on a pixel boundary falls in the
    row *below* it while the same coordinate in x falls in the column to its
    right. The two conventions differ only on the boundary, which is exactly
    where a mosaic test wants to probe.
    """
    return math.ceil(value / RESOLUTION_M) * RESOLUTION_M - RESOLUTION_M / 2


def elevation(x: float, y: float) -> float:
    """A plane, so any pixel's value can be computed rather than looked up."""
    return 400.0 + (x + 470000.0) / 1000.0 + (y + 1104000.0) / 1000.0


def expected_at(x: float, y: float) -> float:
    return float(np.float32(elevation(snap_x(x), snap_y(y))))


def write_candidates(path: Path) -> None:
    """`candidates.parquet` as #6 writes it, both directions of both lines."""
    rows = []
    for index, line in enumerate(LINES_M):
        coords = to_degrees(line)
        node_ids = [10 * index + 1, 10 * index + 2]
        rows.append(candidate(index, node_ids, coords, FORWARD, None))
        rows.append(candidate(index, node_ids[::-1], coords[::-1], REVERSE, None))
    path.parent.mkdir(parents=True, exist_ok=True)
    write_parquet(rows, path)


class Service:
    """A stand-in for ČÚZK's ImageServer that records what it was asked for.

    Every knob here is a way the real service is known to fail while still
    returning HTTP 200: a resampled grid, a response with no `GDAL_NODATA`
    tag, and a window whose tiles are all absent.
    """

    def __init__(
        self,
        *,
        resolution_m: float = float(RESOLUTION_M),
        nodata_tag: bool = True,
        raise_after: int | None = None,
    ) -> None:
        self.urls: list[str] = []
        self.resolution_m = resolution_m
        self.nodata_tag = nodata_tag
        self.raise_after = raise_after

    def __call__(self, url: str, timeout: float, attempts: int) -> bytes:
        if self.raise_after is not None and len(self.urls) >= self.raise_after:
            raise DemError("the connection dropped")
        self.urls.append(url)

        query = query_of(url)
        xmin, ymin, xmax, ymax = (float(v) for v in query["bbox"].replace("%2C", ",").split(","))
        width, height = (int(v) for v in query["size"].replace("%2C", ",").split(","))

        centres_x = xmin + self.resolution_m * (np.arange(width) + 0.5)
        centres_y = ymax - self.resolution_m * (np.arange(height) + 0.5)
        grid_x, grid_y = np.meshgrid(centres_x, centres_y)
        band = elevation(grid_x, grid_y).astype("float32")

        if (xmin, ymin) == (NORTH[0] * SPAN, NORTH[1] * SPAN):
            band[:] = NODATA  # wholly outside coverage, as Poland is
        hx0, hy0, hx1, hy1 = HOLE
        band[(grid_x >= hx0) & (grid_x < hx1) & (grid_y >= hy0) & (grid_y < hy1)] = NODATA
        band[(grid_x == snap_x(ZERO_AT[0])) & (grid_y == snap_y(ZERO_AT[1]))] = 0.0

        profile = {
            "driver": "GTiff",
            "width": width,
            "height": height,
            "count": 1,
            "dtype": "float32",
            "crs": f"EPSG:{DEM_CRS}",
            "transform": Affine(self.resolution_m, 0.0, xmin, 0.0, -self.resolution_m, ymax),
            "tiled": True,
            "blockxsize": SERVER_BLOCK_PX,
            "blockysize": SERVER_BLOCK_PX,
        }
        if self.nodata_tag:
            profile["nodata"] = NODATA
        with rasterio.io.MemoryFile() as memory:
            with memory.open(**profile) as raster:
                raster.write(band, 1)
            return memory.read()


def query_of(url: str) -> dict[str, str]:
    return dict(part.split("=", 1) for part in url.split("?", 1)[1].split("&"))


def run(out: Path, service: Service, **extra) -> int:
    options = {
        "out": out,
        "candidates": out / "candidates.parquet",
        "tile_px": TILE_PX,
        "halo_m": HALO_M,
        "timeout": 5.0,
        "attempts": 1,
        "limit": None,
        "dry_run": False,
        "force": False,
        "window_fetcher": service,
    }
    return fetch(**{**options, **extra})


def manifest_of(out: Path) -> dict:
    return json.loads((out / MANIFEST_NAME).read_text())


@pytest.fixture
def staged(tmp_path: Path) -> Path:
    out = tmp_path / "kraj-1"
    write_candidates(out / "candidates.parquet")
    return out


@pytest.fixture(scope="module")
def fetched(tmp_path_factory) -> tuple[Path, Service]:
    """One complete run, read by the tests that do not mutate it."""
    out = tmp_path_factory.mktemp("fetched") / "kraj-1"
    write_candidates(out / "candidates.parquet")
    service = Service()
    assert run(out, service) == 0
    return out, service


# --- the grid, which is where the negatives bite -----------------------------


def test_tile_index_at_negative_coordinates() -> None:
    span = tile_span(TILE_PX)
    assert span == SPAN

    # int() would truncate toward zero and answer -1835, whose tile starts east
    # of the point.
    assert tile_index(-469900.0, span) == BX
    assert tile_index(-1104000.0, span) == BY

    # A coordinate exactly on an edge belongs to the tile it opens, not the one
    # it closes: the interval is [tx·S, (tx+1)·S).
    assert tile_index(float(BX * span), span) == BX
    assert tile_index(float((BX + 1) * span), span) == BX + 1
    assert tile_index(float(BX * span) - 0.5, span) == BX - 1

    # `plan_tiles` does not call `tile_index` — it cannot, over a million
    # vertices — so pinning the scalar alone would pin a function nothing in
    # production runs. The two spellings of the rule are asserted equal.
    probes = [-469900.0, -1104000.0, float(BX * span), float(BX * span) - 0.5, 0.0, 1.0, -1.0]
    assert list(tile_indices(np.array(probes), span)) == [
        tile_index(value, span) for value in probes
    ]


def test_tile_bboxes_are_grid_anchored() -> None:
    span = tile_span(TILE_PX)
    xmin, ymin, xmax, ymax = tile_bbox(BX, BY, span)

    assert (xmin, ymin, xmax, ymax) == (-470016, -1104128, -469760, -1103872)
    assert all(isinstance(value, int) for value in (xmin, ymin, xmax, ymax))
    assert xmin % span == 0 and ymin % span == 0
    assert (xmax - xmin) // RESOLUTION_M == TILE_PX
    assert (ymax - ymin) // RESOLUTION_M == TILE_PX

    # Neighbours share an edge exactly, which is what lets them mosaic.
    assert tile_bbox(BX + 1, BY, span)[0] == xmax
    assert tile_bbox(BX, BY + 1, span)[1] == ymax


def test_tiles_cover_points_along_segments(staged: Path) -> None:
    planned = plan_tiles(staged / "candidates.parquet", TILE_PX, HALO_M)
    assert planned == PLANNED

    # Every metre of every candidate, plus its halo in each direction, must
    # land in a planned tile — vertices alone would pass on a plan that misses
    # the middle of a long diagonal.
    span = tile_span(TILE_PX)
    for (x0, y0), (x1, y1) in LINES_M:
        steps = int(math.hypot(x1 - x0, y1 - y0)) + 1
        for step in range(steps + 1):
            x = x0 + (x1 - x0) * step / steps
            y = y0 + (y1 - y0) * step / steps
            for dx, dy in ((0, 0), (HALO_M, 0), (-HALO_M, 0), (0, HALO_M), (0, -HALO_M)):
                assert (tile_index(x + dx, span), tile_index(y + dy, span)) in planned


def test_request_shape(fetched: tuple[Path, Service]) -> None:
    _out, service = fetched
    assert len(service.urls) == len(PLANNED)
    for url in service.urls:
        query = query_of(url)
        assert query["bboxSR"] == str(DEM_CRS)
        assert query["imageSR"] == str(DEM_CRS)
        assert query["pixelType"] == "F32"
        assert query["noData"] == "-9999"
        assert query["interpolation"] == "RSP_NearestNeighbor"
        assert query["format"] == "tiff"
        assert query["f"] == "image"

    # `bbox ÷ size` is the resolution, and getting it wrong resamples silently.
    xmin, ymin, xmax, ymax = tile_bbox(BX, BY, SPAN)
    assert f"bbox={xmin}%2C{ymin}%2C{xmax}%2C{ymax}" in window_url(
        (xmin, ymin, xmax, ymax), TILE_PX
    )


# --- windows that arrive wrong -----------------------------------------------

# What ČÚZK's ImageServer actually produces, copied from a real response. The
# GeoTIFF's `ProjectedCSTypeGeoKey` is 5514, but its citation also carries
# ESRI's spelling of Krovák, which GDAL cannot map to a projection method — so
# it degrades the whole CRS to an engineering `LOCAL_CS` and every ordinary way
# of asking for the code comes back empty.
SERVICE_WKT = (
    'LOCAL_CS["S-JTSK / Krovak East North",UNIT["metre",1,AUTHORITY["EPSG","9001"]],'
    'AXIS["Easting",EAST],AXIS["Northing",NORTH],AUTHORITY["EPSG","5514"]]'
)


def test_declared_epsg_reads_a_local_cs() -> None:
    degraded = CRS.from_wkt(SERVICE_WKT)
    assert degraded.to_epsg() is None
    assert degraded.to_authority() is None
    assert degraded != CRS.from_epsg(DEM_CRS)

    # The authority code survives, and it is the file's own declaration.
    assert declared_epsg(degraded) == DEM_CRS
    assert declared_epsg(CRS.from_epsg(DEM_CRS)) == DEM_CRS
    assert declared_epsg(None) is None
    assert declared_epsg(CRS.from_epsg(4326)) != DEM_CRS


def test_rejects_a_resampled_window(staged: Path) -> None:
    service = Service(resolution_m=4.0)
    with pytest.raises(DemError) as raised:
        run(staged, service)
    message = str(raised.value)
    assert "4.0" in message and "asked 2" in message
    assert not list((staged / TILE_DIR).glob("*.tif"))


def test_rejects_a_window_without_nodata(staged: Path) -> None:
    service = Service(nodata_tag=False)
    with pytest.raises(DemError) as raised:
        run(staged, service)
    assert "nodata" in str(raised.value)
    assert not list((staged / TILE_DIR).glob("*.tif"))


def responder(script: list):
    """A fake `urlopen` that plays a script of bodies and errors, in order."""
    remaining = list(script)

    class Response:
        def __init__(self, body: bytes) -> None:
            self.body = body
            self.headers = {"Content-Length": str(len(body))}

        def __enter__(self):
            return self

        def __exit__(self, *_exc) -> bool:
            return False

        def read(self) -> bytes:
            return self.body

    def urlopen(_request, timeout=None):
        item = remaining.pop(0)
        if isinstance(item, Exception):
            raise item
        return Response(item)

    return urlopen


def test_rejects_a_json_error_response(monkeypatch) -> None:
    body = b'{"error":{"code":400,"message":"Invalid or missing input parameters."}}'
    monkeypatch.setattr("urllib.request.urlopen", responder([body]))
    with pytest.raises(DemError) as raised:
        fetch_window("https://example.invalid/exportImage?f=image", 1.0, 1)
    assert "not a GeoTIFF" in str(raised.value)
    assert "Invalid or missing input parameters" in str(raised.value)


def test_retries_a_transient_failure(monkeypatch) -> None:
    good = b"II*\x00" + b"\x00" * 60
    monkeypatch.setattr("krpaly_derive.dem.time.sleep", lambda _seconds: None)
    monkeypatch.setattr(
        "urllib.request.urlopen", responder([OSError("reset"), OSError("reset"), good])
    )
    assert fetch_window("https://example.invalid/exportImage", 1.0, 3) == good


# --- resume, re-entrancy and the manifest ------------------------------------


def test_rerun_fetches_nothing(staged: Path, tmp_path: Path) -> None:
    record = tmp_path / "record"
    first = Service()
    assert run(staged, first, record=record) == 0
    before = manifest_of(staged)
    committed = (record / MANIFEST_NAME).read_bytes()

    second = Service()
    assert run(staged, second, record=record) == 0
    after = manifest_of(staged)

    assert second.urls == []
    assert after["run"]["reused"] == len(PLANNED)
    del before["run"], after["run"]
    assert before == after
    # The verification re-run #21 says would churn: the working copy's `run`
    # moves, and the committed copy does not change by a byte.
    assert (record / MANIFEST_NAME).read_bytes() == committed
    assert json.loads(committed) == after


def test_resume_refetches_only_what_is_missing(staged: Path) -> None:
    assert run(staged, Service()) == 0
    (staged / TILE_DIR / tile_name(*EAST)).unlink()

    service = Service()
    assert run(staged, service) == 0
    assert len(service.urls) == 1
    assert f"bbox={EAST[0] * SPAN}%2C" in service.urls[0]
    assert manifest_of(staged)["run"] == {**manifest_of(staged)["run"], "fetched": 1, "reused": 2}


def test_corrupt_tile_is_refetched(staged: Path) -> None:
    assert run(staged, Service()) == 0
    victim = staged / TILE_DIR / tile_name(*BASE)
    victim.write_bytes(victim.read_bytes()[:1024])

    service = Service()
    assert run(staged, service) == 0
    assert len(service.urls) == 1
    assert manifest_of(staged)["run"]["complete"] is True


def test_tile_on_disk_without_a_manifest_entry_is_refetched(staged: Path) -> None:
    """A crash between `os.replace` and the manifest rewrite costs one tile."""
    assert run(staged, Service()) == 0
    written = manifest_of(staged)
    written["tiles"] = [entry for entry in written["tiles"] if (entry["tx"], entry["ty"]) != BASE]
    (staged / MANIFEST_NAME).write_text(json.dumps(written))

    service = Service()
    assert run(staged, service) == 0
    assert len(service.urls) == 1
    assert f"bbox={BASE[0] * SPAN}%2C" in service.urls[0]


def test_limit_leaves_the_run_incomplete(staged: Path) -> None:
    assert run(staged, Service(), limit=2) == 1
    partial = manifest_of(staged)
    assert partial["run"]["complete"] is False
    assert partial["run"]["limit"] == 2
    assert partial["counts"]["tiles"] == len(PLANNED)
    assert partial["counts"]["tiles_present"] == 2
    assert partial["output"]["vrt"] is None
    assert not (staged / OUTPUT_NAME).exists()

    # `--limit` counts what needed fetching, so a second limited run advances
    # rather than re-verifying the same two forever.
    service = Service()
    assert run(staged, service, limit=2) == 0
    assert len(service.urls) == 1
    finished = manifest_of(staged)
    assert finished["run"]["complete"] is True
    assert finished["counts"]["tiles_present"] == len(PLANNED)
    # The two tiles from the limited run kept their checksums.
    assert {entry["sha256"] for entry in partial["tiles"]} <= {
        entry["sha256"] for entry in finished["tiles"]
    }


def test_an_incomplete_mosaic_is_never_recorded(staged: Path, tmp_path: Path) -> None:
    """The record drops `run.complete`, so a record existing has to mean complete."""
    record = tmp_path / "record"
    assert run(staged, Service(), limit=2, record=record) == 1
    assert not (record / MANIFEST_NAME).exists()


def test_crash_leaves_a_readable_manifest(staged: Path) -> None:
    with pytest.raises(DemError):
        run(staged, Service(raise_after=2))

    partial = manifest_of(staged)
    assert partial["run"]["complete"] is False
    assert partial["counts"]["tiles_present"] == 2
    assert not (staged / OUTPUT_NAME).exists()
    assert not list((staged / TILE_DIR).glob("*.tmp"))


def test_unplanned_tiles_are_kept_and_counted(staged: Path, capsys) -> None:
    """A plan that shrank is not a licence to delete a file the operator has."""
    assert run(staged, Service()) == 0
    stray = staged / TILE_DIR / tile_name(BX + 9, BY + 9)
    stray.write_bytes(b"II*\x00 not planned by anything")
    capsys.readouterr()

    assert run(staged, Service()) == 0
    assert stray.exists()
    assert manifest_of(staged)["run"]["unplanned_on_disk"] == [stray.name]
    assert "this plan does not name, kept" in capsys.readouterr().err


def test_grid_change_is_refused(staged: Path) -> None:
    assert run(staged, Service()) == 0
    service = Service()
    with pytest.raises(DemError) as raised:
        run(staged, service, tile_px=TILE_PX * 2)
    assert "different grid" in str(raised.value)
    assert service.urls == []


def test_dry_run_writes_nothing(staged: Path, capsys) -> None:
    service = Service()
    assert run(staged, service, dry_run=True) == 0
    assert service.urls == []
    assert not (staged / TILE_DIR).exists()
    assert not (staged / MANIFEST_NAME).exists()
    assert f"plan: {len(PLANNED)} tiles" in capsys.readouterr().err


def test_tile_px_ceiling(staged: Path) -> None:
    for bad, expected in ((4224, "maxImageHeight"), (1000, "multiple")):
        with pytest.raises(SystemExit) as raised:
            main(["--out", str(staged), "--tile-px", str(bad)])
        assert expected in str(raised.value)
    assert not (staged / TILE_DIR).exists()


def test_main_runs_on_its_defaults(staged: Path, monkeypatch) -> None:
    """The argparse defaults, exercised — including `<out>/candidates.parquet`.

    `fetch` resolves its fetcher at call time rather than binding one as a
    default argument, which is what lets this reach the CLI without a socket.
    """
    service = Service()
    monkeypatch.setattr("krpaly_derive.dem.fetch_window", service)
    monkeypatch.setattr("krpaly_derive.dem.DEFAULT_TILE_PX", TILE_PX)
    monkeypatch.setattr("krpaly_derive.dem.DEFAULT_HALO_M", HALO_M)

    assert main(["--out", str(staged)]) == 0
    assert len(service.urls) == len(PLANNED)
    assert manifest_of(staged)["source"]["file"] == "candidates.parquet"
    assert manifest_of(staged)["run"]["complete"] is True


def test_missing_candidates(tmp_path: Path) -> None:
    with pytest.raises(SystemExit) as raised:
        main(["--out", str(tmp_path / "nowhere")])
    assert "candidates.parquet" in str(raised.value)


# --- what the counts say -----------------------------------------------------


def test_nodata_counted_not_absorbed(fetched: tuple[Path, Service]) -> None:
    out, _service = fetched
    written = manifest_of(out)
    by_tile = {(entry["tx"], entry["ty"]): entry for entry in written["tiles"]}

    hole_px = int((HOLE[2] - HOLE[0]) / RESOLUTION_M) * int((HOLE[3] - HOLE[1]) / RESOLUTION_M)
    assert by_tile[BASE]["nodata_px"] == hole_px
    assert by_tile[BASE]["min_m"] is not None

    # A window wholly outside coverage is nodata throughout, and says so
    # rather than reading as a sea-level plateau.
    assert by_tile[NORTH]["nodata_px"] == TILE_PX * TILE_PX
    assert by_tile[NORTH]["min_m"] is None
    assert written["counts"]["tiles_all_nodata"] == 1

    # 0.0 is not an elevation in Czechia, so it is counted rather than averaged
    # into the terrain.
    assert by_tile[EAST]["zero_px"] == 1
    assert written["counts"]["zero_px"] == 1
    assert written["counts"]["px_total"] == len(PLANNED) * TILE_PX * TILE_PX


def test_fetch_time_survives_the_verification_rerun(staged: Path) -> None:
    """#7 asks for the wall clock, then asks for a re-run that would erase it.

    The fetch duration accumulates across resumes and is preserved by a run
    that fetches nothing, so the figure the ticket wants outlives the
    verification the same ticket mandates.
    """
    assert run(staged, Service(), limit=2) == 1
    first = manifest_of(staged)["dem"]["fetch_wall_clock_s"]
    assert first > 0

    assert run(staged, Service()) == 0
    second = manifest_of(staged)["dem"]["fetch_wall_clock_s"]
    assert second > first  # the resumed tile added to it, rather than replacing it

    assert run(staged, Service()) == 0
    assert manifest_of(staged)["dem"]["fetch_wall_clock_s"] == second


def test_records_which_transform_proj_chose(fetched: tuple[Path, Service]) -> None:
    """A `Transformer` cannot describe itself, so the group is asked instead."""
    out, _service = fetched
    recorded = manifest_of(out)["grid"]["transform"]
    assert recorded["operations_available"] >= 1
    assert "Krovak" in recorded["operation"]
    assert recorded["operation_accuracy_m"] is not None
    assert recorded["proj_version"]


def test_records_the_crs_the_windows_declare(fetched: tuple[Path, Service]) -> None:
    """#7: "the CRS as the files actually declare it" — not the one asked for."""
    out, _service = fetched
    declared = manifest_of(out)["dem"]["crs_declared_as"]
    assert "5514" in declared
    assert manifest_of(out)["dem"]["crs"] == DEM_CRS


def test_tiles_are_listed_in_grid_order(fetched: tuple[Path, Service]) -> None:
    out, _service = fetched
    written = manifest_of(out)
    assert [(entry["tx"], entry["ty"]) for entry in written["tiles"]] == PLANNED


# --- the mosaic --------------------------------------------------------------


def test_vrt_geotransform_and_extent(fetched: tuple[Path, Service]) -> None:
    out, _service = fetched
    with rasterio.open(out / OUTPUT_NAME) as mosaic:
        # Two tiles across, two down — the gap is inside the rectangle.
        assert (mosaic.width, mosaic.height) == (2 * TILE_PX, 2 * TILE_PX)
        assert mosaic.crs.to_epsg() == DEM_CRS
        # The top edge is the *least negative* row's upper bound. Reverse the
        # flip and the mosaic reads real elevations upside down.
        assert mosaic.transform.c == float(BX * SPAN)
        assert mosaic.transform.f == float((BY + 2) * SPAN)
        assert (mosaic.transform.a, mosaic.transform.e) == (float(RESOLUTION_M), -RESOLUTION_M)

    xml = (out / OUTPUT_NAME).read_text()
    assert '<SourceFilename relativeToVRT="1">dem/x-1836_y-4312.tif</SourceFilename>' in xml
    assert f'<DstRect xOff="0" yOff="0" xSize="{TILE_PX}" ySize="{TILE_PX}"/>' in xml
    assert f'<DstRect xOff="0" yOff="{TILE_PX}" xSize="{TILE_PX}" ySize="{TILE_PX}"/>' in xml
    assert "HideNoDataValue" not in xml


def test_vrt_reads_the_mosaic(fetched: tuple[Path, Service]) -> None:
    out, _service = fetched
    probes = [(-469900.0, -1104000.0), (-469700.0, -1104060.0)]
    with rasterio.open(out / OUTPUT_NAME) as mosaic:
        read = [float(value[0]) for value in mosaic.sample(probes)]
    assert read == [expected_at(x, y) for x, y in probes]


def test_vrt_nodata_is_visible(fetched: tuple[Path, Service]) -> None:
    out, _service = fetched
    with rasterio.open(out / OUTPUT_NAME) as mosaic:
        assert mosaic.nodata == NODATA


def test_vrt_gap_reads_nodata(fetched: tuple[Path, Service]) -> None:
    """Unfetched ground reads −9999, not 0.0.

    Candidate-driven coverage is deliberately full of holes. A mosaic that
    reads them as sea level turns every candidate crossing one into a
    spectacular fictional climb — which is the whole reason this stage exists.
    """
    out, _service = fetched
    inside_the_gap = (GAP[0] * SPAN + 100.0, GAP[1] * SPAN + 100.0)
    inside_the_hole = (HOLE[0] + 1.0, HOLE[1] + 1.0)
    with rasterio.open(out / OUTPUT_NAME) as mosaic:
        read = [float(value[0]) for value in mosaic.sample([inside_the_gap, inside_the_hole])]
    assert read == [NODATA, NODATA]
