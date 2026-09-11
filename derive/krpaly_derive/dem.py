"""Stage 2: candidate polylines in, the DMR 5G windows they need out.

    uv run --directory derive python -m krpaly_derive.dem --out data/kraj-1

Fetches 2 m terrain from ČÚZK's ImageServer for exactly the ground #6's
candidates cross, verifies each window against the grid it was asked for,
mosaics what arrives behind a single VRT, and writes a manifest precise enough
to re-obtain the pixels. It does not sample the terrain — resampling polylines
to 10 m and deciding what to do about a candidate that crosses a hole are #8.

`extract.py` says outright that it does not download, because "wrapping it
would put a network path inside a batch stage and inside its tests". That
still holds for one 945 MB file the operator `curl`s once. It does not hold
for a few thousand windows chosen from the geometry, so the network path lives
here — and the seam that keeps it out of the tests is `window_fetcher`, a
parameter of `fetch` defaulting to the real one.

Two properties are the reason this stage exists as its own file on disk:

* **Nodata is reported, never absorbed.** The default response is fictional in
  two ways at once — uncovered pixels arrive as `0.0`, and whole 128 px blocks
  the server omits arrive as nothing at all — so `noData=-9999` is mandatory,
  every window is opened and checked before it is written, and the mosaic
  carries the nodata value too. Candidate-driven coverage is *deliberately*
  full of holes; a VRT without `<NoDataValue>` reads unfetched ground as sea
  level, which is the window-level defect `INPUTS.md` measured, at mosaic
  scale.
* **Idempotent and resumable.** Tiles are large and the connection will drop.
  The manifest is the durable state: a re-run checksums what is on disk,
  fetches only what is missing or wrong, and re-verifies everything.

`dem.vrt` is on the order of 4×10⁹ pixels. It exists to be *window*-read and a
bare `read()` over it would materialise ~17 GB. #8 should also raise
`GDAL_MAX_DATASET_POOL_SIZE` above its default of 100: with a few thousand
sources and point sampling in candidate order, every read otherwise re-opens a
`.tif`, which is the difference between minutes and hours that choosing a VRT
here was supposed to settle.
"""

from __future__ import annotations

import argparse
import json
import math
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

import numpy as np
import pyarrow.parquet as pq
import pyproj
import rasterio
import shapely
from pyproj import Transformer
from pyproj.transformer import TransformerGroup

# This stage consumes #6's output, so the two facts it needs about that file —
# how a forward row spells its direction, and how a checksum over it is taken —
# are read from the module that writes it rather than said again here. A copy
# of either is a place for the two stages to drift apart.
from krpaly_derive.extract import FORWARD, sha256_of

# The export route, and the only one available: the service's capabilities are
# `Catalog,Mensuration,Image,Metadata` — there is no `Download` operation, and
# `exportTilesAllowed` is false. See derive/INPUTS.md § DEM source.
EXPORT_URL = "https://ags.cuzk.gov.cz/arcgis2/rest/services/dmr5g/ImageServer/exportImage"

# Native in and out, so the server never reprojects: S-JTSK / Krovák East
# North horizontally, Bpv vertically. Both are recorded in the manifest
# because #5's `derivation` row carries them as columns.
DEM_CRS = 5514
VERTICAL_CRS = 8357

# The service's own grid. `bbox ÷ size` must equal this exactly or the
# response — still a valid GeoTIFF — is a resampled lie.
RESOLUTION_M = 2

# Mandatory, not a default. Without it uncovered pixels arrive as 0.0 with
# nothing in the file to say so, and a candidate crossing that region becomes a
# spectacular sea-level climb. `NODATA_PARAM` is the string the URL and the VRT
# carry; the float is what rasterio compares against.
NODATA_PARAM = "-9999"
NODATA = -9999.0

# The service's response tiling, measured. A window whose side is not a
# multiple of this pays for padding it cannot use — a 200 px probe returned
# 65 536 samples for the 40 000 asked. Asserted on arrival rather than assumed,
# because it is also what the VRT tells GDAL about its sources.
SERVER_BLOCK_PX = 128

# 1024 px is 2.048 km, 4.2 MB and 1.44 s measured — a multiple of the block
# size, and small enough that a failure late in a transfer is cheap. The
# service's ceiling is 15 000 × 4 100 px.
DEFAULT_TILE_PX = 1024
MAX_TILE_PX = 4100

# Measured throughput, used only to project a `--dry-run` figure.
THROUGHPUT_BYTES_S = 3_000_000

# The half-pixel that lets #8's bilinear read at a tile edge reach its
# neighbour needs only 1 m. The other 31 absorb the ~1 m of disagreement
# between the WGS84 → S-JTSK operations different PROJ installs choose, so the
# planned tile set does not depend on which one is present. Do not "optimise"
# this to 2.0.
DEFAULT_HALO_M = 32.0

# The grid is measured from here rather than from the candidates' bounding
# box. That is what makes windows mosaic without half-pixel drift, and makes
# two runs — or two kraje — ask for byte-identical bboxes over the same ground.
GRID_ORIGIN = (0, 0)

# WGS84, spelled as the CRS the GeoParquet #6 writes declares by omission.
SOURCE_CRS = "OGC:CRS84"
PROJECTED_CRS = f"EPSG:{DEM_CRS}"

SOURCE_NAME = "candidates.parquet"
OUTPUT_NAME = "dem.vrt"
MANIFEST_NAME = "dem.manifest.json"
TILE_DIR = "dem"

# The ArcGIS REST version, pinned from INPUTS.md rather than re-read at run
# time: a live probe is a second HTTP path the test seam does not cover, and a
# value ČÚZK can move under a run that is otherwise deterministic.
SERVICE_VERSION = 12

DEM_PRODUCT = "ZABAGED® – Výškopis – DMR 5G (64111), via the ImageServer 2 m mosaic"
DEM_ROUTE = EXPORT_URL

USER_AGENT = "krpaly-derive/0.0.0 (+https://github.com/Kooozel/krpaly)"

# How often the manifest is rewritten mid-run. Small enough that a connection
# dropping at 90 % costs one tile rather than the run.
MANIFEST_EVERY = 25

# Both byte orders, because the check that matters is "did a GeoTIFF arrive at
# all": an ArcGIS error is an HTTP 200 with `Content-Type: image/tiff` and a
# JSON body, so nothing above the bytes can be trusted to notice.
TIFF_MAGIC = (b"II*\x00", b"MM\x00*")

BACKOFF_S = 1.0


class DemError(Exception):
    """An input or a response this stage cannot derive from, said in one line."""


@dataclass(frozen=True)
class WindowStats:
    """What one validated window holds, counted in the same read that checked it.

    `min_m`/`max_m` are over everything that is not nodata — zeros included,
    deliberately, so a window that quietly lost its `noData` parameter shows up
    as a 0.0 minimum rather than hiding inside the average.
    """

    nodata_px: int
    zero_px: int
    min_m: float | None
    max_m: float | None
    crs_declared_as: str


def tile_span(tile_px: int) -> int:
    """Metres on a side. An int, so every bbox this module produces is exact."""
    return tile_px * RESOLUTION_M


def tile_index(value: float, span: int) -> int:
    """Which tile a coordinate falls in.

    `math.floor`, never `int()`: Czech EPSG:5514 coordinates are negative in
    both axes, and `int()` truncates toward zero — `int(-470000 / 2048)` is
    −229, whose tile starts *east* of the point. Every vertex in the western
    half of a tile would be filed one tile over, and the hole would appear one
    tile away from the road that caused it.
    """
    return math.floor(value / span)


def tile_indices(values: np.ndarray, span: int) -> np.ndarray:
    """`tile_index` over an array, which is how the planner actually asks.

    Split out rather than inlined so the rule has exactly one statement of
    itself: a test that pins `tile_index` at negative coordinates and leaves
    the planner computing its own `np.floor` would be pinning a function
    nothing in production calls.
    """
    return np.floor(values / span).astype(np.int64)


def tile_bbox(tx: int, ty: int, span: int) -> tuple[int, int, int, int]:
    """The half-open square `[tx·S, (tx+1)·S) × [ty·S, (ty+1)·S)`, in whole metres.

    Integers end to end — into the URL and into the manifest — which is what
    keeps two runs asking for byte-identical windows without anyone having to
    decide how a float is formatted.
    """
    x0, y0 = tx * span, ty * span
    return x0, y0, x0 + span, y0 + span


def tile_name(tx: int, ty: int) -> str:
    """`x-230_y-540.tif`. The prefixes are not decoration.

    Every index here is negative, and `-230_-540.tif` is a filename that every
    command line in existence reads as a flag.
    """
    return f"x{tx}_y{ty}.tif"


def transformer() -> Transformer:
    """WGS84 → EPSG:5514, the same pair and direction `extract.py` buffers in.

    `always_xy` because without it pyproj honours EPSG:4326's declared
    latitude-first axis order and every tile lands in the Baltic.

    This lets PROJ choose its operation per point, which is fine for planning
    — the halo absorbs the metres between operations — and wrong for
    sampling. `sample.py` pins one operation instead; see derive/INPUTS.md
    § The transform #8 samples through.
    """
    return Transformer.from_crs(SOURCE_CRS, PROJECTED_CRS, always_xy=True)


def transform_provenance() -> dict:
    """Which WGS84 → S-JTSK operation PROJ will choose here, and how good it is.

    A `Transformer` reports nothing about itself — `description`, `definition`
    and `accuracy` are all the string "unavailable until proj_trans is called",
    and stay that way after transforming, because PROJ defers the choice to
    transform time. `TransformerGroup` is where the answer lives: it lists the
    operations the installed PROJ can actually run, best first, and that list
    changes with the datum grids present on the machine.

    The spread between them is metres — the best available here is accurate to
    6 m — which is comfortably inside the halo, so *coverage* is safe. But the
    planned tile **set** can still differ by one tile between two machines, and
    recording this is what makes that a manifest diff rather than a mystery.
    """
    group = TransformerGroup(SOURCE_CRS, PROJECTED_CRS, always_xy=True)
    best = group.transformers[0] if group.transformers else None
    return {
        "proj_version": pyproj.proj_version_str,
        "operation": best.description if best else None,
        "operation_accuracy_m": best.accuracy if best else None,
        "operations_available": len(group.transformers),
    }


def segments_in_metres(candidates: Path, to_metres: Transformer) -> tuple[np.ndarray, ...]:
    """Every forward candidate's consecutive coordinate pairs, in metres.

    Vectorised throughout: a kraj is on the order of a million vertices, and
    calling `Transformer.transform` once per vertex is minutes of pure Python
    in a stage whose other costs are measured in bytes.
    """
    table = pq.read_table(candidates, columns=["geometry", "direction"])
    directions = table.column("direction").to_pylist()
    wkb = table.column("geometry").to_pylist()
    lines = shapely.from_wkb(
        [blob for blob, direction in zip(wkb, directions, strict=True) if direction == FORWARD]
    )
    if len(lines) == 0:
        return (np.empty(0),) * 4

    coords = shapely.get_coordinates(lines)
    x, y = to_metres.transform(coords[:, 0], coords[:, 1])

    # A vertex starts a segment unless it is the last of its own line, so the
    # per-line ragged structure collapses to one boolean mask over one array.
    per_line = shapely.get_num_coordinates(lines)
    starts_a_segment = np.ones(len(x), dtype=bool)
    starts_a_segment[np.cumsum(per_line) - 1] = False
    head = np.nonzero(starts_a_segment)[0]
    return x[head], y[head], x[head + 1], y[head + 1]


def subdivide(
    x0: np.ndarray, y0: np.ndarray, x1: np.ndarray, y1: np.ndarray, max_step: float
) -> tuple[np.ndarray, ...]:
    """Cut segments longer than half a tile into equal pieces.

    A segment is planned by its bounding box, which over-covers by `O(len²)`
    for a long diagonal: two nodes 20 km apart on a straight road would pull in
    a hundred tiles to cross fourteen. Czech ways are mostly tens of metres
    between nodes, so this touches very few segments and is done in Python for
    exactly those.
    """
    length = np.hypot(x1 - x0, y1 - y0)
    long = length > max_step
    if not long.any():
        return x0, y0, x1, y1

    keep = ~long
    parts: list[list[np.ndarray]] = [[x0[keep]], [y0[keep]], [x1[keep]], [y1[keep]]]
    pieces = np.ceil(length[long] / max_step).astype(np.int64)
    for ax0, ay0, ax1, ay1, count in zip(
        x0[long], y0[long], x1[long], y1[long], pieces, strict=True
    ):
        step = np.linspace(0.0, 1.0, count + 1)
        px, py = ax0 + (ax1 - ax0) * step, ay0 + (ay1 - ay0) * step
        parts[0].append(px[:-1])
        parts[1].append(py[:-1])
        parts[2].append(px[1:])
        parts[3].append(py[1:])
    return tuple(np.concatenate(part) for part in parts)


def plan_tiles(candidates: Path, tile_px: int, halo_m: float) -> list[tuple[int, int]]:
    """Which grid tiles the candidates need, in ascending `(tx, ty)`.

    Coverage is candidate-driven rather than polygon-driven: the DEM exists to
    be sampled along polylines, so the windows worth asking for are the ones
    holding candidate geometry, and the buffered kraj's ≈12 GB upper bound is
    never fetched. Emission order is the determinism guarantee, as it is in #6.
    """
    span = tile_span(tile_px)
    x0, y0, x1, y1 = subdivide(*segments_in_metres(candidates, transformer()), max_step=span / 2)

    lo_x = np.minimum(x0, x1) - halo_m
    hi_x = np.maximum(x0, x1) + halo_m
    lo_y = np.minimum(y0, y1) - halo_m
    hi_y = np.maximum(y0, y1) + halo_m

    # `ceil(hi/S) - 1` rather than `floor(hi/S)` is the half-open upper bound:
    # the two agree except when `hi` lands exactly on a tile edge, where floor
    # would pull in a tile the box only touches. `maximum` covers the
    # degenerate box that is entirely on one edge.
    tx_lo = tile_indices(lo_x, span)
    ty_lo = tile_indices(lo_y, span)
    tx_hi = np.maximum(np.ceil(hi_x / span).astype(np.int64) - 1, tx_lo)
    ty_hi = np.maximum(np.ceil(hi_y / span).astype(np.int64) - 1, ty_lo)

    # The overwhelming majority of segments are shorter than a tile and fall in
    # one, so that case never enters a Python loop.
    tiles: set[tuple[int, int]] = set()
    single = (tx_lo == tx_hi) & (ty_lo == ty_hi)
    tiles.update(zip(tx_lo[single].tolist(), ty_lo[single].tolist(), strict=True))
    spread = ~single
    for a, b, c, d in zip(
        tx_lo[spread].tolist(),
        tx_hi[spread].tolist(),
        ty_lo[spread].tolist(),
        ty_hi[spread].tolist(),
        strict=True,
    ):
        for tx in range(a, b + 1):
            for ty in range(c, d + 1):
                tiles.add((tx, ty))
    return sorted(tiles)


def window_url(bbox: tuple[int, int, int, int], tile_px: int) -> str:
    """derive/INPUTS.md § "The request shape #7 must use", verbatim.

    Split out from the fetch so the contract is a pure function a test can
    assert on without a socket — getting any of it wrong resamples silently.
    """
    xmin, ymin, xmax, ymax = bbox
    query = urllib.parse.urlencode(
        {
            "bbox": f"{xmin},{ymin},{xmax},{ymax}",
            "bboxSR": DEM_CRS,
            "imageSR": DEM_CRS,
            "size": f"{tile_px},{tile_px}",
            "format": "tiff",
            "pixelType": "F32",
            "interpolation": "RSP_NearestNeighbor",
            "noData": NODATA_PARAM,
            "f": "image",
        }
    )
    return f"{EXPORT_URL}?{query}"


def fetch_window(url: str, timeout: float, attempts: int) -> bytes:
    """One window, over `urllib` — no new HTTP dependency for one GET in a loop.

    Two failures the service reports as HTTP 200 and which must be raised
    rather than written: an ArcGIS error body, which arrives labelled
    `image/tiff` and is only detectable by the absent TIFF magic, and a
    response shorter than its own `Content-Length`.
    """
    last: Exception | None = None
    for attempt in range(attempts):
        if attempt:
            time.sleep(BACKOFF_S * 2 ** (attempt - 1))
        try:
            request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
            with urllib.request.urlopen(request, timeout=timeout) as response:
                declared = response.headers.get("Content-Length")
                data = response.read()
            if declared is not None and len(data) != int(declared):
                raise DemError(f"truncated: {len(data)} bytes of the {declared} promised")
            if not data.startswith(TIFF_MAGIC):
                body = data[:200].decode("utf-8", "replace")
                raise DemError(f"not a GeoTIFF — the service said: {body}")
            return data
        except (OSError, DemError) as error:
            last = error
    raise DemError(f"{attempts} attempts failed — {last}")


def declared_epsg(crs: rasterio.crs.CRS | None) -> int | None:
    """The EPSG code a window declares, however GDAL managed to parse it.

    `crs.to_epsg()` alone is not enough on this service, and finding that out
    cost a real request. The response's `ProjectedCSTypeGeoKey` is 5514, but
    its citation also carries ESRI's spelling of Krovák — `X_Scale`,
    `XY_Plane_Rotation` — which GDAL cannot map to a projection method, so it
    degrades the whole CRS to a `LOCAL_CS` and `to_epsg()`, `to_authority()`
    and a comparison against `CRS.from_epsg(5514)` all come back empty. The
    authority code survives in the WKT regardless, and it is the file's own
    declaration either way, so that is what is read.
    """
    if crs is None:
        return None
    code = crs.to_epsg()
    if code is not None:
        return code
    # The last authority in a WKT belongs to its outermost object, which is
    # the CRS itself.
    found = re.findall(r'AUTHORITY\["EPSG","(\d+)"\]', crs.to_wkt())
    return int(found[-1]) if found else None


def validate(data: bytes, bbox: tuple[int, int, int, int], tile_px: int) -> WindowStats:
    """Check a window against the grid it was asked for, before it is written.

    Every fault is collected rather than raised at the first, so the message
    names both grids and an operator does not fix one mismatch to discover the
    next. The nodata check is the one that is not belt-and-braces: without the
    `GDAL_NODATA` tag the file cannot tell uncovered ground from sea level.
    """
    xmin, _ymin, _xmax, ymax = bbox
    faults: list[str] = []
    with rasterio.io.MemoryFile(data) as memory, memory.open() as window:
        if (window.width, window.height) != (tile_px, tile_px):
            faults.append(f"{window.width}×{window.height} px, asked {tile_px}×{tile_px}")
        grid = window.transform
        if (grid.a, grid.e) != (float(RESOLUTION_M), -float(RESOLUTION_M)):
            faults.append(f"{grid.a}×{-grid.e} m pixels, asked {RESOLUTION_M}×{RESOLUTION_M}")
        if (grid.c, grid.f) != (float(xmin), float(ymax)):
            faults.append(f"origin ({grid.c}, {grid.f}), asked ({xmin}, {ymax})")
        epsg = declared_epsg(window.crs)
        if epsg != DEM_CRS:
            faults.append(f"CRS EPSG:{epsg} ({window.crs}), asked EPSG:{DEM_CRS}")
        if window.dtypes[0] != "float32":
            faults.append(f"{window.dtypes[0]} samples, asked float32")
        if window.nodata != NODATA:
            faults.append(
                f"nodata {window.nodata}, asked {NODATA} — an untagged window "
                "cannot tell uncovered ground from sea level"
            )
        # PixelIsPoint would shift the transform by half a pixel and move every
        # sample in the database a metre, while still looking like a valid file.
        if window.tags().get("AREA_OR_POINT") != "Area":
            faults.append(f"AREA_OR_POINT {window.tags().get('AREA_OR_POINT')!r}, asked 'Area'")
        if tuple(window.block_shapes[0]) != (SERVER_BLOCK_PX, SERVER_BLOCK_PX):
            faults.append(
                f"{window.block_shapes[0]} blocks, asked "
                f"{SERVER_BLOCK_PX}×{SERVER_BLOCK_PX} — the VRT tells GDAL otherwise"
            )
        if faults:
            raise DemError("; ".join(faults))
        band = window.read(1)
        crs_wkt = window.crs.to_wkt()

    real = band[band != NODATA]
    return WindowStats(
        nodata_px=int(np.count_nonzero(band == NODATA)),
        # 0.0 is never a valid Czech elevation — the country's lowest point is
        # 115 m — so a zero means the `noData` parameter silently stopped
        # applying. Counted and reported; whether such a candidate is dropped
        # or interpolated across is #8's decision, not this stage's.
        zero_px=int(np.count_nonzero(band == 0.0)),
        min_m=float(real.min()) if real.size else None,
        max_m=float(real.max()) if real.size else None,
        crs_declared_as=crs_wkt,
    )


def build_vrt(entries: list[dict], tile_px: int, path: Path) -> None:
    """One `<SimpleSource>` per fetched tile, over a deliberately sparse grid.

    Hand-written rather than through `gdal.BuildVRT`, which needs `osgeo` —
    something rasterio does not expose — or the `gdalbuildvrt` CLI, which is
    not a pinnable dependency. Written with f-strings rather than
    `minidom.toprettyxml`, whose whitespace has moved between Python versions
    and would move this file's sha256 with it.

    `<NoDataValue>` on the band is what makes an unfetched gap read −9999:
    GDAL pre-fills a VRT read buffer with the band's nodata when one is set and
    not hidden, and with zeros otherwise. `<HideNoDataValue>` is therefore
    absent on purpose — it would keep the fill but hide the value from
    `GetNoDataValue`, handing #8 −9999 as if it were terrain. There is no
    `<NODATA>` element here either: that belongs to `<ComplexSource>` and is
    silently ignored inside a `<SimpleSource>`. The tiles do not overlap, so a
    tile's own nodata copies through verbatim, which is what is wanted.
    """
    if not entries:
        raise DemError("no tiles to mosaic — no candidate geometry reached the grid")

    span = tile_span(tile_px)
    tx_min = min(entry["tx"] for entry in entries)
    tx_max = max(entry["tx"] for entry in entries)
    ty_min = min(entry["ty"] for entry in entries)
    ty_max = max(entry["ty"] for entry in entries)

    # `ty_max` is the *least negative* index and therefore the northernmost
    # row, which is where the mosaic's top edge is. Reverse the two and the
    # VRT still opens, still reads real elevations, and is upside down.
    top = (ty_max + 1) * span
    sources = "".join(
        f"""    <SimpleSource>
      <SourceFilename relativeToVRT="1">{entry["file"]}</SourceFilename>
      <SourceBand>1</SourceBand>
      <SourceProperties RasterXSize="{tile_px}" RasterYSize="{tile_px}" """
        f"""DataType="Float32" BlockXSize="{SERVER_BLOCK_PX}" BlockYSize="{SERVER_BLOCK_PX}"/>
      <SrcRect xOff="0" yOff="0" xSize="{tile_px}" ySize="{tile_px}"/>
      <DstRect xOff="{(entry["tx"] - tx_min) * tile_px}" """
        f"""yOff="{(ty_max - entry["ty"]) * tile_px}" xSize="{tile_px}" ySize="{tile_px}"/>
    </SimpleSource>
"""
        for entry in entries
    )
    # EPSG by code rather than an embedded WKT, so the file's bytes do not
    # depend on the installed PROJ — the same reasoning that leaves `crs`
    # absent from #6's GeoParquet metadata.
    xml = (
        f'<VRTDataset rasterXSize="{(tx_max - tx_min + 1) * tile_px}" '
        f'rasterYSize="{(ty_max - ty_min + 1) * tile_px}">\n'
        f'  <SRS dataAxisToSRSAxisMapping="1,2">EPSG:{DEM_CRS}</SRS>\n'
        f"  <GeoTransform>{float(tx_min * span)}, {float(RESOLUTION_M)}, 0.0, "
        f"{float(top)}, 0.0, {float(-RESOLUTION_M)}</GeoTransform>\n"
        f'  <VRTRasterBand dataType="Float32" band="1">\n'
        f"    <NoDataValue>{NODATA_PARAM}</NoDataValue>\n"
        f"    <ColorInterp>Gray</ColorInterp>\n"
        f"{sources}  </VRTRasterBand>\n"
        f"</VRTDataset>\n"
    )
    write_atomically(path, xml.encode("utf-8"))


def write_atomically(path: Path, data: bytes) -> None:
    """Through a temporary file and `os.replace`, as `write_parquet` does.

    An interrupted transfer must leave nothing half-written: #8 checks for a
    tile's existence, not its integrity, and a truncated manifest would mean no
    checksums for anything and a re-fetch of the whole mosaic.
    """
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_bytes(data)
    os.replace(tmp, path)


def tile_entry(tx: int, ty: int, span: int, path: Path, stats: WindowStats) -> dict:
    return {
        "tx": tx,
        "ty": ty,
        "bbox": list(tile_bbox(tx, ty, span)),
        "file": f"{TILE_DIR}/{tile_name(tx, ty)}",
        "sha256": sha256_of(path),
        "bytes": path.stat().st_size,
        "nodata_px": stats.nodata_px,
        "zero_px": stats.zero_px,
        "min_m": stats.min_m,
        "max_m": stats.max_m,
    }


def read_manifest(path: Path) -> dict:
    """The previous run's manifest, or an empty one.

    Load-bearing for more than checksums: a reused tile's pixel statistics are
    carried forward from here rather than recomputed, because recomputing means
    decoding the whole mosaic on a run that fetched nothing. A manifest that
    cannot be read proves nothing, so the tiles it described are re-fetched.
    """
    try:
        prior = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError):
        return {}
    return prior if isinstance(prior, dict) else {}


def grid_block(tile_px: int, halo_m: float) -> dict:
    """Everything about the grid that decides where a tile's edges fall."""
    return {
        "origin_x": GRID_ORIGIN[0],
        "origin_y": GRID_ORIGIN[1],
        "tile_px": tile_px,
        "tile_m": tile_span(tile_px),
        "halo_m": halo_m,
        "transform": transform_provenance(),
    }


def check_arguments(candidates: Path, tile_px: int, halo_m: float) -> None:
    if not candidates.is_file():
        raise DemError(
            f"{candidates} is not a file — run krpaly_derive.extract first, or pass --candidates"
        )
    if tile_px % SERVER_BLOCK_PX or tile_px <= 0:
        raise DemError(
            f"--tile-px {tile_px} is not a multiple of {SERVER_BLOCK_PX} — the service tiles its "
            "response at that size and a window that is not a multiple pays for padding"
        )
    if tile_px > MAX_TILE_PX:
        raise DemError(f"--tile-px {tile_px} exceeds the service's maxImageHeight of {MAX_TILE_PX}")
    if halo_m < RESOLUTION_M:
        raise DemError(
            f"--halo-m {halo_m} is under one pixel — #8's bilinear read at a tile edge needs its "
            f"neighbour planned, which takes at least {RESOLUTION_M} m"
        )


def report(written: dict, fetched: int, reused: int) -> None:
    """What the run cost and what it holds, on stderr, as `extract.py` reports.

    The disk figure and the accumulated fetch time are #7's deliverable, so
    they are said out loud rather than left for someone to read out of the
    manifest.
    """
    counts = written["counts"]
    print(
        f"tiles: {counts['tiles_present']}/{counts['tiles']} present, "
        f"{fetched} fetched, {reused} reused, {counts['bytes_total'] / 1e9:.2f} GB",
        file=sys.stderr,
    )
    print(
        f"nodata: {counts['nodata_px']} of {counts['px_total']} px "
        f"({100 * counts['nodata_px'] / max(counts['px_total'], 1):.1f}%), "
        f"{counts['tiles_all_nodata']} tiles wholly uncovered",
        file=sys.stderr,
    )
    if counts["zero_px"]:
        print(
            f"zero: {counts['zero_px']} px at exactly 0.0 — 0 m is not an elevation in Czechia, "
            "so the noData parameter is not being applied. #8 must not sample these",
            file=sys.stderr,
        )
    print(
        f"fetching: {written['dem']['fetch_wall_clock_s']} s in total, "
        f"this run {written['run']['wall_clock_s']} s",
        file=sys.stderr,
    )


def fetch(
    out: Path,
    candidates: Path,
    tile_px: int,
    halo_m: float,
    timeout: float,
    attempts: int,
    limit: int | None,
    dry_run: bool,
    force: bool,
    window_fetcher: Callable[[str, float, int], bytes] | None = None,
) -> int:
    # Resolved here rather than as a default argument, which would bind at
    # import time and leave `main` — and therefore every argparse default —
    # untestable without a socket.
    window_fetcher = window_fetcher or fetch_window
    check_arguments(candidates, tile_px, halo_m)

    started = time.monotonic()
    started_at = datetime.now(UTC).isoformat(timespec="seconds")
    span = tile_span(tile_px)

    plan = plan_tiles(candidates, tile_px, halo_m)
    if not plan:
        raise DemError(f"{candidates} holds no forward candidate geometry — nothing to fetch")

    projected = len(plan) * tile_px * tile_px * 4
    print(
        f"plan: {len(plan)} tiles of {span} m, "
        f"≈{projected / 1e9:.1f} GB, ≈{projected / THROUGHPUT_BYTES_S / 60:.0f} min at 3 MB/s",
        file=sys.stderr,
    )
    if dry_run:
        # Deliberately before anything is created and before anything is
        # hashed: the point of this mode is the number, cheaply, and
        # re-verifying an existing mosaic would defeat it.
        return 0

    out.mkdir(parents=True, exist_ok=True)
    tile_dir = out / TILE_DIR
    tile_dir.mkdir(exist_ok=True)
    manifest_path = out / MANIFEST_NAME
    vrt_path = out / OUTPUT_NAME

    prior = read_manifest(manifest_path)
    prior_grid = prior.get("grid", {})
    grid = grid_block(tile_px, halo_m)
    # The source's checksum is provenance rather than a gate: a changed
    # candidates file changes *which* tiles are wanted, and the ones already on
    # disk are still correct pixels. A changed grid is different: it changes
    # the geometry of every tile on disk, and without this the old ones become
    # `unplanned_on_disk` — kept, per the rule below — silently doubling the
    # footprint while the run reports success.
    geometry_keys = ("origin_x", "origin_y", "tile_px", "halo_m")
    if prior_grid and not force:
        changed = [key for key in geometry_keys if prior_grid.get(key) != grid[key]]
        if changed:
            raise DemError(
                f"{manifest_path} was built on a different grid ({', '.join(changed)}) — the tiles "
                "in it are not on this one. Pass --force, or use a different --out"
            )

    # An interrupted run leaves these behind, and nothing else ever removes
    # them. Only `.tif` is globbed elsewhere, so they are invisible rather than
    # harmful — but they are still bytes on a disk this stage is measuring.
    for stale in tile_dir.glob("*.tif.tmp"):
        stale.unlink()

    prior_tiles = {(entry["tx"], entry["ty"]): entry for entry in prior.get("tiles", [])}
    entries: dict[tuple[int, int], dict] = {}
    fetched = reused = 0

    prior_dem = prior.get("dem", {})
    fetched_at = prior_dem.get("fetched_at")
    # Accumulated across resumes and preserved by a run that fetches nothing,
    # because #7 asks for the wall clock as a deliverable and the run that
    # produces it is followed, by the same ticket, by a verification re-run
    # that would otherwise overwrite it with its own few seconds.
    fetch_wall_clock_s = prior_dem.get("fetch_wall_clock_s") or 0.0
    crs_declared_as = prior_dem.get("crs_declared_as")

    source_sha256 = sha256_of(candidates)
    planned_names = {tile_name(tx, ty) for tx, ty in plan}
    unplanned = sorted(
        path.name for path in tile_dir.glob("*.tif") if path.name not in planned_names
    )
    if unplanned:
        # Never deleted: this stage does not get to decide that a file the
        # operator has is garbage. It is a shrunken plan, not a mistake.
        print(f"on disk: {len(unplanned)} tiles this plan does not name, kept", file=sys.stderr)

    def manifest(complete: bool) -> dict:
        ordered = [entries[key] for key in sorted(entries)]
        px_total = len(ordered) * tile_px * tile_px
        return {
            "dem": {
                "product": DEM_PRODUCT,
                "route": DEM_ROUTE,
                "resolution_m": RESOLUTION_M,
                "crs": DEM_CRS,
                "vertical_crs": VERTICAL_CRS,
                "nodata_value": NODATA,
                "service_version": SERVICE_VERSION,
                # The time of the last *fetch*, not of the last run: a re-run
                # that fetches nothing must not move a provenance timestamp,
                # or "the manifest minus `run` is identical" stops being true
                # and #5's `dem_fetched_at` starts lying.
                "fetched_at": started_at if fetched else fetched_at,
                "fetch_wall_clock_s": (
                    round(fetch_wall_clock_s + time.monotonic() - started, 3)
                    if fetched
                    else fetch_wall_clock_s
                ),
                # What the windows themselves say, which is not the constant
                # that was asked for: this service declares EPSG:5514 through a
                # citation GDAL degrades to a LOCAL_CS, and the record of that
                # belongs in the manifest rather than only in a docstring.
                "crs_declared_as": crs_declared_as,
            },
            "grid": grid,
            "source": {
                "file": candidates.name,
                "sha256": source_sha256,
                "bytes": candidates.stat().st_size,
            },
            "tiles": ordered,
            "counts": {
                "tiles": len(plan),
                "tiles_present": len(ordered),
                "bytes_total": sum(entry["bytes"] for entry in ordered),
                "px_total": px_total,
                "nodata_px": sum(entry["nodata_px"] for entry in ordered),
                "zero_px": sum(entry["zero_px"] for entry in ordered),
                "tiles_all_nodata": sum(1 for entry in ordered if entry["min_m"] is None),
            },
            "output": {
                "vrt": OUTPUT_NAME if complete else None,
                "sha256": sha256_of(vrt_path) if complete else None,
                "dir": TILE_DIR,
            },
            # The only block allowed to differ between two runs over the same
            # input, which is what makes re-entrancy assertable on the rest.
            # `fetched`/`reused` live here rather than in `counts` because they
            # are facts about a run — the first is N/0 and the second 0/N —
            # rather than about the data.
            "run": {
                "started_at": started_at,
                "wall_clock_s": round(time.monotonic() - started, 3),
                "complete": complete,
                "limit": limit,
                "fetched": fetched,
                "reused": reused,
                "unplanned_on_disk": unplanned,
                "candidates_path": str(candidates.resolve()),
            },
        }

    def save(complete: bool) -> None:
        write_atomically(
            manifest_path,
            (json.dumps(manifest(complete), indent=2, sort_keys=True) + "\n").encode("utf-8"),
        )

    try:
        for index, (tx, ty) in enumerate(plan):
            path = tile_dir / tile_name(tx, ty)
            known = prior_tiles.get((tx, ty))
            # "Present" is not "correct". The checksum is re-read every run —
            # tens of seconds against the hours a re-fetch costs — and it is
            # the only thing that makes "already there" mean "already right".
            # A tile with no entry is the crash-between-write-and-rewrite case
            # and re-fetches, which costs one tile.
            if not force and path.is_file() and known and sha256_of(path) == known["sha256"]:
                entries[(tx, ty)] = known
                reused += 1
            else:
                # `--limit N` is the first N tiles that *need* fetching, not
                # the first N of the plan: otherwise a second `--limit 20`
                # re-verifies the same twenty forever and never advances.
                if limit is not None and fetched >= limit:
                    continue
                bbox = tile_bbox(tx, ty, span)
                data = window_fetcher(window_url(bbox, tile_px), timeout, attempts)
                stats = validate(data, bbox, tile_px)
                write_atomically(path, data)
                entries[(tx, ty)] = tile_entry(tx, ty, span, path, stats)
                crs_declared_as = stats.crs_declared_as
                fetched += 1
                if fetched % MANIFEST_EVERY == 0:
                    save(complete=False)
                    print(
                        f"tiles: {index + 1}/{len(plan)}, {fetched} fetched, {reused} reused",
                        file=sys.stderr,
                    )
    finally:
        if len(entries) < len(plan):
            # The manifest must never claim a plan it has not finished: #8 and
            # #11 would consume the half-mosaic happily, and it would look
            # fine, because the missing half reads as legitimate nodata. Its
            # own failure is caught so it cannot replace a DemError with an
            # OSError traceback.
            try:
                save(complete=False)
            except OSError as error:  # pragma: no cover - a disk that is also full
                print(f"manifest: could not be written — {error}", file=sys.stderr)

    # VRT first, then its checksum, then the manifest that records it: dying
    # between the two costs a cheap rebuild, while the other order would record
    # a sha256 for a file that does not exist. Rebuilt on every completed run
    # even when nothing was fetched, so a hand-deleted tile cannot leave a
    # source dangling.
    ordered = [entries[key] for key in sorted(entries)]
    complete = len(ordered) == len(plan)
    if complete:
        build_vrt(ordered, tile_px, vrt_path)
    save(complete=complete)

    written = manifest(complete)
    report(written, fetched, reused)
    if not complete:
        print(f"incomplete: no {OUTPUT_NAME} written — re-run to finish", file=sys.stderr)
        return 1
    print(f"mosaic: {vrt_path}", file=sys.stderr)
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="krpaly_derive.dem", description=__doc__)
    parser.add_argument("--out", required=True, type=Path, help="the stage directory #6 wrote")
    parser.add_argument(
        "--candidates",
        type=Path,
        default=None,
        help=f"candidate polylines to plan against (default: <out>/{SOURCE_NAME})",
    )
    parser.add_argument(
        "--tile-px",
        type=int,
        default=DEFAULT_TILE_PX,
        help=f"window side in pixels, a multiple of {SERVER_BLOCK_PX} (default: {DEFAULT_TILE_PX})",
    )
    parser.add_argument(
        "--halo-m",
        type=float,
        default=DEFAULT_HALO_M,
        help=f"metres of margin around each candidate (default: {DEFAULT_HALO_M:.0f})",
    )
    parser.add_argument("--timeout", type=float, default=120.0, help="seconds per request")
    parser.add_argument("--attempts", type=int, default=4, help="tries per window before giving up")
    parser.add_argument(
        "--limit", type=int, default=None, help="stop after this many tiles are fetched"
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="print the plan and its projected cost, fetch nothing",
    )
    parser.add_argument(
        "--force", action="store_true", help="re-fetch every planned tile, and accept a new grid"
    )
    args = parser.parse_args(argv)

    # A mistyped path or a window that came back on the wrong grid is the
    # operator's problem to see on one line, not a traceback to read.
    try:
        return fetch(
            out=args.out,
            candidates=args.candidates or args.out / SOURCE_NAME,
            tile_px=args.tile_px,
            halo_m=args.halo_m,
            timeout=args.timeout,
            attempts=args.attempts,
            limit=args.limit,
            dry_run=args.dry_run,
            force=args.force,
        )
    except DemError as error:
        raise SystemExit(f"dem: {error}") from error


if __name__ == "__main__":
    raise SystemExit(main())
