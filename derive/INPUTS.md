# Derivation inputs, pinned

A derivation is reproducible only if its inputs are named exactly. There are three: the OSM extract,
the terrain, and the engine build. #1 pins the engine — tag *and* commit SHA, read out of the
release's `VERSION` asset — and [Engine build](#engine-build) records how it is vendored. This
document pins the other two, and settles what "in the kraj" means for a way that crosses the
border.

Almost every value below was read back from the source rather than copied from a product page, and
the [Re-verify](#re-verify) block at the end is the set of commands that read them again — all eight
of them run clean as written. The handful that a machine-readable source does not carry are marked
where they appear. A few values a document cannot hold — they need the whole file — and those name
the run's own manifest instead: #6 writes `candidates.manifest.json`, #7 `dem.manifest.json`, and
each is committed, minus its `run` block, at `derive/manifests/<name of --out>/`.

Scope: this names inputs. The `derivation` table is #5, the OSM extraction is #6, the DEM fetcher
and its manifest are #7.

## OSM snapshot

| Field | Value |
| --- | --- |
| URL | `https://download.geofabrik.de/europe/czech-republic-260901.osm.pbf` |
| bytes | 945 133 839 |
| published md5 | `aa4815fbd1de50fd7249ff0eaa98ba9d` |
| `osmosis_replication_timestamp` | `2026-09-01T20:20:50Z` (epoch 1788294050) |
| `osmosis_replication_sequence_number` | 4897 |
| `osmosis_replication_base_url` | `https://download.geofabrik.de/europe/czech-republic-updates` |
| `writingprogram` | `osmium/1.16.0` |
| sha256 | recorded per run by #6, in `candidates.manifest.json` → `osm_snapshot.sha256` |

Geofabrik publishes md5, not sha256, so the digest in the `derivation` row is ours: #6 computes the
sha256 of the bytes it downloaded and records both, the published md5 proving the download was not
corrupted and the sha256 identifying the file afterwards.

**Why the monthly archive and not a daily.** `-latest` is not a pin at all — it moves daily, so two
derivations a week apart are not comparable and the difference is invisible. But a dated *daily*
extract is barely better as a pin, and by a much larger margin than it looks: on 2026-09-09 the
`europe/` listing held exactly **seven** dated dailies, `260902` through `260908`. A daily link
recorded in a `derivation` row stops resolving within a week or two — long before the milestone is
finished. The `YYMM01` monthly archives are the ones kept: seventeen of them in the same listing,
back to `140101`. The monthly file is the only one of the three that is both fixed and still there
next year.

**The header, without downloading a gigabyte.** The replication timestamp is the fact about the
data, and it lives inside the file rather than in its name. It is readable from the first 64 KB: a
big-endian `int32` length, then the `BlobHeader`, then the `OSMHeader` blob, whose zlib-compressed
`HeaderBlock` carries `writingprogram` (field 16), `osmosis_replication_timestamp` (32),
`_sequence_number` (33) and `_base_url` (34). The script in [Re-verify](#re-verify) is how the
values above were obtained and how a reader checks them.

## Kraj boundary

| Field | Value |
| --- | --- |
| relation | `442461` |
| `name` | Moravskoslezský kraj |
| `admin_level` | 4 |
| `boundary` | administrative |
| `ref:nuts` | CZ080 |
| `ISO3166-2` | CZ-80 |
| `wikidata` | Q190550 |
| members | 318 (311 `outer` ways, 6 `subarea`, 1 `admin_centre`) |
| live version | 267 — `2026-08-03T21:13:18Z`, changeset 186892310, read 2026-09-09 |
| version in the extract | recorded per run by #6, in `candidates.manifest.json` → `boundary.relation_version` |

The last two rows are the load-bearing distinction. An administrative boundary is an editable object
like any other: the live relation keeps moving, and one of the failure modes this milestone exists
to surface is a climb count that changed because the boundary did. **The version a `derivation` row
records is the version present in the pinned `.pbf`, not the live one** — the derivation reads the
snapshot, so #6 reads the version out of the extract. Version 267 is written down here only so that
a later divergence between the snapshot and the live relation is visible rather than assumed away.

### Border policy: buffer and assign by summit

A climb that starts in Moravskoslezský and tops out in Zlínský is a real climb, so the three
candidate policies are not equal:

| Policy | Consequence |
| --- | --- |
| clip geometry at the boundary | truncates real climbs — a partial profile is a wrong profile, not a shorter one |
| include every intersecting way | duplicates the same climb at all thirteen future kraj borders |
| **buffer the extract, assign by summit** | **chosen** |

Extract with a **10 km buffer** around the relation, run detection over the buffered geometry, and
assign each finished climb to a kraj by the position of its **summit**. Nothing is truncated, and
each climb lands in exactly one kraj no matter how many derivations see it. 10 km comfortably
exceeds the longest plausible Czech climb, so no candidate is cut off mid-ascent; #6 may revise the
figure once it has real geometry to measure against.

`boundary_buffer_m` and `boundary_assignment` are columns rather than constants (see [Provenance
columns](#provenance-columns-for-5)) so that a later change of policy shows up in the data instead
of only in git history.

## Cyclable ways

Which ways the extraction rides is the one decision in #6 that moves the headline climb count, so
it is versioned and recorded rather than left implicit in a filter expression. The predicate is
`derive/krpaly_derive/cyclable.py`; its name is `cyclable/v2`, and every run writes that name into
`candidates.manifest.json` → `way_filter.version`. The manifests under `derive/manifests/kraj-1/`
were derived under `cyclable/v1`, and those under `derive/manifests/kraj-1-v2/` under v2; v1 itself
is recoverable from git history, not from `main`'s current tree.

| | `highway`, and the tags that qualify it |
| --- | --- |
| in | `motorway_link` `trunk`/`_link` `primary`/`_link` `secondary`/`_link` `tertiary`/`_link` `unclassified` `residential` `living_street` `road` `cycleway` |
| in | `service` **only** with no `service=*` subtype |
| in | `track` where `tracktype` is `grade1` or `grade2`, **or** `surface` is paved — `asphalt` `chipseal` `concrete` `concrete:lanes` `concrete:plates` `paving_stones` |
| out | `motorway`, and everything not named above — `path` `footway` `steps` `bridleway` `pedestrian` `corridor` `construction` `proposed` `raceway` `busway` `platform` among them |
| out | `service` with any subtype — `driveway` `parking_aisle` `drive-through` `alley` among them |
| out | `track` with neither a `grade1`/`grade2` `tracktype` nor a paved `surface` |
| out | `access` in `private`/`no`, and `bicycle=no` |
| override | `bicycle` in `yes`/`designated`/`permissive` beats an `access` exclusion, but never promotes a `highway` value that is out, *including a `service` subtype* — a `path` signed for bicycles is still not a road climb, and nor is a `service=driveway` |
| note | `motor_vehicle` is not consulted: `motor_vehicle=no`/`private` on an in-road leaves it in, because a road closed to cars is still ridden |

`motorway` is out and `motorway_link` is in on purpose: the motorway itself is not ridden, but a
link is often the only cyclable connection between two roads that are.

**Why plain `service` is in.** The Beskydy summit roads are asphalt forest roads mapped as plain
`highway=service`, gated for cars by `access=permissive` or `motor_vehicle=no`/`private`. #24
found Lysá hora's last 2.5 km, 10.5–13 km along the climb, on nothing else, so under v1 there was
no route to the summit at all. The subtype is what tells such a road from the service noise:
`driveway`, `parking_aisle`, `drive-through` and `alley` are ways nobody climbs, and they are
exactly the ones mappers subtype.

**Why tracks are mostly out**, recorded because it is re-litigable. krpaly is a road-climb database
and this milestone's headline number is the climb count, so an untagged forest track — the modal
`track` in the Beskydy — must not inflate it. `grade1` and `grade2` are the surfaced ones. A paved
`surface` is let in too, whatever the `tracktype`, because `surface` is the fact `tracktype` only
approximates; `sett` and `cobblestone` are paved but left out, since a forest track tagged that way
is noise rather than a road climb. The exception does not rescue the untagged Bílý Kříž track #24
names, which carries neither tag.

**What v1 was**, so the kraj-1 manifests stay readable: no `service` at all, and a `track` only on
`grade1`/`grade2`, with `surface` never consulted. Everything else is as above.

Widening any of this is the next version and a new derivation rather than an edit to an existing
one, which is exactly the visibility wanted: two derivations that differ only by their predicate
are two sets of rows, told apart by the manifest.

### Structures

DMR 5G is bare earth. Read under every sample, a viaduct profiles the valley it spans and a tunnel
the hill it passes through, and climb-engine detects either as a climb with total confidence. So
extraction also records whether a ridden way is off the ground, in `candidates.parquet` →
`structure`, by `structure_of` in the same module. Its name is `structure/v1`, written into
`candidates.manifest.json` → `way_filter.structure`.

| `structure` | read from |
| --- | --- |
| `bridge` | `bridge` with any value but `no` — `yes`, `viaduct`, `boardwalk` … |
| `tunnel` | `tunnel` with any value but `no` — `yes`, `building_passage`, `culvert` … |
| `covered` | `covered` with any value but `no` |
| null | none of the three, or only `=no` — the ground |

The first that applies wins, in that order. The combinations are real — a covered bridge is
`bridge=yes` + `covered=yes` — but all three are profiled alike, so the order decides only which
of `segments_bridge`, `segments_tunnel` and `segments_covered` a candidate is counted in.

**The rule is per candidate** because the tags are on the way and #6 splits only *within* a way:
every candidate is wholly on a structure or wholly off one, and none needs cutting. `sample.py`
reads the DEM at a structure candidate's two ends — where it meets the ground — and interpolates
linearly by distance between them; `profiles.manifest.json` → `sampling.structure_rule` records
this. The straight line between the abutments is the deck, which flattens the deck's own vertical
curve and camber: metres truer than a valley floor, and not exact. The ends are still terrain, so an
end on nodata still drops the candidate.

**A separate version, not a `cyclable` bump.** The ridden set is unchanged, and a `cyclable` bump
is kept for widening it. A manifest written before this key existed lacks it, so extraction
re-derives rather than reusing candidates that have no `structure` column — and `sample.py`
refuses such a file outright.

## DEM source

### The product

| Field | Value |
| --- | --- |
| product | ZABAGED® – Výškopis – DMR 5G, obchodní kód 64111 |
| what it is | heights of discrete points in an irregular triangulated network (TIN) — **not a raster** |
| native delivery | LAZ, per SM5 map sheet, S-JTSK + Bpv |
| accuracy | úplná střední chyba **0,18 m** open terrain, **0,3 m** forested |
| provenance | airborne laser scanning 2009–2013, completed 2016-06-30, verified continuously since |
| licence | CC BY 4.0, no fee |

The TIN characterisation, both accuracy figures and the scanning years were read back from the
`dmr5g` service's own `description` field rather than from a product page, and the block below
re-reads them: *"…výšek diskrétních bodů v nepravidelné trojúhelníkové síti (TIN) … s úplnou střední
chybou výšky 0,18 m v odkrytém terénu a 0,3 m v zalesněném terénu … v letech 2009 až 2013."* The
commercial code, the LAZ/SM5 delivery form, the 2016-06-30 completion date and the licence come from
the ČÚZK product pages during the survey.

DMR 5G has **no published raster product**. Code 64111 delivers LAZ only. The 2 m figure that gets
quoted for DMR 5G is the cell size of ČÚZK's ImageServer mosaic — a service *derived from* DMR 5G,
not a DMR 5G product. For Czech climbs the accuracy number that matters is the forested one, 0,3 m:
most of what this database will contain is under trees.

### The route chosen for kraj-1: the ImageServer 2 m export

kraj-1 is a POC — the milestone exists to find out whether the approach works at all — and this
route reaches first climbs fastest without leaving the official channels: the DMR 5G product page
lists it under *Stahování dat* as "Export výřezu dat".

| Field | Value (measured 2026-09-09) |
| --- | --- |
| endpoint | `https://ags.cuzk.gov.cz/arcgis2/rest/services/dmr5g/ImageServer` |
| grid | `pixelSizeX` 2, `pixelSizeY` 2, `bandCount` 1, `pixelType` F32 |
| CRS | `latestWkid` 5514 (S-JTSK / Krovák East North), `wkid` 102067, vertical `vcsWkid` 8357 (Bpv) |
| service extent | x −904 703.6 … −431 605.6, y −1 227 414.12 … −935 118.12 |
| per-request ceiling | `maxImageWidth` 15 000 × `maxImageHeight` 4 100 px = 30 × 8.2 km |
| `exportTilesAllowed` | `false` — `maxDownloadImageCount` 20, `maxDownloadSizeLimit` 2048 MB |
| capabilities | `Catalog,Mensuration,Image,Metadata` — there is no `Download` operation |
| `copyrightText` | `© ČÚZK` |
| `currentVersion` | 12 |
| vintage | **none published** — see below |

**The recorded limitation: this route has no vintage.** The mosaic's catalog holds exactly three
rows — `dmr5g_raster` (LowPS 2, HighPS 128) and two overview pyramids — so it is a single bulk
render, not a live per-sheet mosaic. `editFieldsInfo` is `null`, and the only date anywhere on the
service is `<CreaDate>20240826</CreaDate>`, in the ESRI metadata at `.../ImageServer/info/metadata`
— which dates the metadata record rather than the raster, and is absent altogether from the ISO
19139 and FGDC renderings of the same record. So `dem_source` **cannot be made third-party
re-obtainable on this route** — only reproducible for us, via the per-window checksums in #7's
manifest. That is the same class of defect as Geofabrik's `-latest`, and it is accepted deliberately
here rather than overlooked.

**Switch trigger.** Move to the LAZ route (ATOM `DMR5G-SJTSK`, gridded to 2 m in our own code, TIN
linear interpolation) when kraj-1 has proved the pipeline and provenance starts to matter, or sooner
if a spot-check shows the mosaic disagreeing with the raw points. Switching costs a re-derive, which
the `derivation_id` design already accommodates.

### The request shape #7 must use

Getting this wrong resamples silently — the response is still a valid GeoTIFF, just not the grid
that was asked for.

```
GET .../ImageServer/exportImage
    bbox=<window>&bboxSR=5514&imageSR=5514    native in and out, no server reprojection
    size=<w>,<h>                              exactly bbox ÷ 2 m, or the server resamples
    format=tiff&pixelType=F32&f=image
    interpolation=RSP_NearestNeighbor
    noData=-9999                              mandatory — see Nodata below
```

Anchor every window's bbox to one global 2 m grid from a fixed origin, so windows mosaic without
half-pixel drift, and assert on arrival that the returned `ImageWidth`, `ImageLength`,
`ModelPixelScale` and `ModelTiepoint` equal what was requested. A verified 200 × 200 px probe over
Ostrava returns `ModelPixelScale (2, 2, 0)`, `ModelTiepoint (0, 0, 0, xmin, ymax, 0)`,
`SampleFormat` 3 (IEEE float), and a GeoTIFF CRS of `S-JTSK / Krovak East North`.

### Nodata, measured

`CLAUDE.md` requires that nodata be reported and never absorbed. On this service that is not a
precaution against a hypothetical hole — the default response *is* fictional, in two different ways
at once. Both were measured on one window straddling the Polish border north of Ostrava, requested
twice at **native 2 m** and differing only in the `noData` parameter:

```
bbox=-470000,-1089248,-467952,-1085152&size=1024,2048     2.048 × 4.096 km, 2 097 152 px
```

| | without `noData` | with `noData=-9999` |
| --- | --- | --- |
| pixels in tiles the server omits entirely | 1 736 704 (106 of 128 tiles) | 1 736 704 |
| uncovered pixels inside present tiles | 101 957, valued **0.0** | 101 957, valued −9999 |
| `GDAL_NODATA` tag (42113) | **absent** | present |
| real terrain | 258 491 px, 202.60–216.72 m | identical |

Both responses carry `ModelPixelScale (2, 2, 0)`, so nothing here is an artefact of resampling: the
three counts sum to the full 2 097 152, and only **12 %** of the window is terrain.

Two consequences, both mandatory for #7:

1. **Always pass `noData=-9999`.** Without it, uncovered pixels arrive as `0.0` with nothing in the
   file to say so. A candidate crossing that region samples as a sea-level plateau and becomes a
   spectacular fictional climb. The real data is byte-identical either way, so the parameter costs
   nothing.
2. **Absent tiles are nodata too.** The response is a *sparse* tiled GeoTIFF: `TileOffsets` and
   `TileByteCounts` are `0` for any 128 × 128 block with no data, and a window entirely outside
   coverage comes back HTTP 200 as a valid, correctly georeferenced, 1.4 KB file with every tile
   absent. A reader that skips absent tiles without filling them yields whatever its buffer was
   initialised with — zeros, in most array libraries. Fill them with the nodata value explicitly.

A useful cross-check on both: **0.0 is never a valid elevation in Czechia.** The country's lowest
point is 115 m (Hřensko), and Moravskoslezský's is about 195 m (Bohumín), so any sample at exactly
0.0 is nodata regardless of how it arrived. Count them per window and decide explicitly, per
`CLAUDE.md`, whether such candidates are dropped or interpolated across.

#8 settles it: **dropped whole, counted by reason** (`nodata_whole`, `nodata_partial`), so a hole
never becomes a sea-level climb and the geometry never drifts off its start and end nodes. A
structure's interior is never read (§ Structures), so only its two ends can be nodata.

### The transform #8 samples through

The mosaic is EPSG:5514 and #6's candidates are WGS84, and PROJ 9.8.1 offers four operations
between the two here: S-JTSK to WGS 84 (1), (3), (4) and (5). `Transformer.from_crs` chooses among
them **per point**. At 18.5 E 49.55 N it uses (4), Slovakia's, whose area of use ends at 49.61 N,
inside Moravskoslezský. North of that line it uses a Czech one. The operations disagree by up to
~2.5 m, so an unpinned transform puts a metre-scale step through the middle of the kraj.

`sample.py` therefore pins **EPSG:5239**, "S-JTSK to WGS 84 (5)": Czechia, 1 m, a seven-parameter
Helmert with no grid, so it gives the same answer on every machine. It is selected by the EPSG id
of the operation's datum step, never by searching the operation's JSON for "5239": that string also
occurs inside (1), EPSG:1623. Both directions are the one pipeline run forward and inverse, and
they round-trip to ~1e-8° (~1 mm), not to the bit, because PROJ's inverse Helmert is approximate.
#7 planned its tiles with PROJ's own choice rather than the pin. Its 32 m halo absorbs the
difference, and `profiles.manifest.json` records both.

### Volume

At a 2 m grid and F32 the arithmetic is clean: one sample per 4 m², four bytes each — **1 byte per
m², so 1 MB per km²** of uncompressed TIFF.

| Extent | Area | Uncompressed |
| --- | --- | --- |
| kraj only | 5 431 km² | ≈ 5.4 GB |
| kraj + 10 km buffer (polygon) | ≈ 12 100 km² | ≈ 12 GB |
| bounding box of the above | ≈ 17 600 km² | ≈ 17.6 GB |

The buffered-polygon figure is `A + Pd + πd²` over the kraj's area (5 430.54 km², Wikidata
Q190550, the same entity the relation's own `wikidata` tag names) and the outer-ring perimeter
measured from the relation's geometry, 633 km. That perimeter is read off the full-detail boundary,
which a 10 km buffer smooths out, so the figure errs high — as an upper bound should.

The bbox figure is what a naive rectangular tiling costs instead: Moravskoslezský is elongated, so
its bounding box is 123 × 103 km — 2.3× the kraj's own area — and buffering that to 143 × 123 km
wastes about a third of the download on Poland and Slovakia.

**These are upper bounds on what #7 actually needs to fetch**, and the gap is large. The DEM is only
sampled along extracted polylines, so the windows worth requesting are the ones that contain
candidate geometry, not the ones that tile the region.

Window sizing is #7's call, but two measured constraints bear on it. The ceiling window,
15 000 × 4 100 px, is 246 km² and a **246 MB** single response — large enough that a failure late
in the transfer is expensive. And the response is tiled at 128 × 128, so a window whose width or
height is not a multiple of 128 px pays for padding it cannot use: the 200 × 200 probe returned
65 536 samples for the 40 000 requested, 39 % waste. **Size windows in multiples of 128 px
(256 m).**

### Rejected alternatives

Recorded with reasons so this is not re-litigated:

| Product | Gen | Why not |
| --- | --- | --- |
| INSPIRE EL-GRID (64113) | **4G** | The obvious download, and the trap. It is served as `INSPIRE_Nadmorska_vyska` at **5 × 5 m** — byte-for-byte the grid of the `dmr4g` service and not the 2 m of `dmr5g`, which is what gives the generation away without reading a word of the product page. DMR 4G's own accuracy is **1 m forested** against DMR 5G's 0,3 m, so this route puts most of a metre of error into the database under a DMR 5G label. |
| ZABAGED DMR 4G TIFF (64110) | 4G | Wrong generation, at least honestly labelled — the `dmr4g` service description says 4G outright. |
| INSPIRE EL-TIN (64114) | 5G | GML, about 1.6× bulkier than the equivalent LAZ, and its tiles were last updated well behind the LAZ ones — and it still needs gridding. (Bulk and tile dates are from the ČÚZK product pages during the survey, not re-read by the block below.) |
| ZABAGED DMR 5G LAZ (64111) | 5G | The eventual target; deferred past the POC because it adds a gridding step and a new dependency. See the switch trigger above. |

## Provenance columns for #5

#5's sketch has single `osm_snapshot` and `dem_source` columns. Each of the values above is several
facts, so they should be explicit scalars — queryable, self-documenting, and a wrong value is
visible in a column rather than buried in a blob:

```
osm_snapshot_url, osm_snapshot_sha256, osm_snapshot_replication_ts, osm_snapshot_seq
boundary_relation_id, boundary_relation_version, boundary_buffer_m, boundary_assignment
dem_product, dem_route, dem_resolution_m, dem_crs, dem_vertical_crs,
  dem_nodata_value, dem_manifest_sha256, dem_fetched_at
engine_version, engine_commit                                          (from #1)
engine_config_override                                                 (from #9)
```

`boundary_relation_version` holds the version found in the extract, not the live one.
`boundary_assignment` holds `summit`, so a later change of policy is a visible change of data.
`dem_nodata_value` holds `-9999` and exists because a row derived before that parameter was passed
is not comparable to one derived after. Per-window checksums stay in #7's committed manifest; the
row carries `dem_manifest_sha256`, the sha256 of the committed
`derive/manifests/<run>/dem.manifest.json` — the manifest minus `run`, so a verification re-run
does not move it.

## Engine build

climb-engine is a pinned input like the snapshot and the tiles. Detection output is the contract, so
the same geometry through a different build is different climbs. `derive/vendor/climb-engine/` holds
one release's **library** and its `VERSION` asset. It does not hold `climb-cli.mjs`: that reads GPX
and emits ride JSON, the wrong entry point for a DEM profile, and vendoring it invites its use.

| | |
| --- | --- |
| Release | `v0.1.0` |
| Commit | `9fb96def4e9f9d9a3487c1c4701246ec1c42579d` |
| Built for | Node 20, esbuild ESM bundle — the floor CI runs |
| Called as | `detectClimbs(tuples, { config })` from `derive/engine/harness.mjs`, scored with `aso` |

`krpaly_derive.detect` reads the tag and the commit out of `VERSION` into its manifest's
`derivation` block, which is what `derivation.engine_version` and `engine_commit` receive (#1).
Nobody types them. A tag deleted or re-pointed later still leaves the SHA. The library's sha256 is
recorded beside them, so a vendored file edited in place shows up as a digest no release has. The
override krpaly passes over the build's defaults is `ENGINE_CONFIG_OVERRIDE` in `detect.py`,
recorded as `derivation.engine_config_override`.

To adopt another release, re-vendor rather than edit, and then re-derive. The stage signature
includes the library's digest, so the next run does that by itself:

```sh
rm -r derive/vendor/climb-engine
gh release download <tag> --repo Kooozel/climb-engine \
  --pattern climb-engine.mjs --pattern VERSION --dir derive/vendor/climb-engine
echo "vendored_on:   $(date -I)" >> derive/vendor/climb-engine/VERSION
```

While the version is 0.x, a release whose minor moved is a detector whose output changed. Read its
notes before adopting it.

## Re-verify

Each block reads one pinned value back from its source. They need only `curl` and Python 3.

**The `.pbf` header, over a 64 KB range request:**

```sh
python3 - <<'PY'
import datetime, urllib.request, zlib

URL = "https://download.geofabrik.de/europe/czech-republic-260901.osm.pbf"
buf = urllib.request.urlopen(
    urllib.request.Request(URL, headers={"Range": "bytes=0-65535"}), timeout=60
).read()

def fields(b):
    i = 0
    while i < len(b):
        k = s = 0
        while True:
            c = b[i]; i += 1; k |= (c & 0x7F) << s; s += 7
            if not c & 0x80: break
        num, wire = k >> 3, k & 7
        v = s = 0
        while True:
            c = b[i]; i += 1; v |= (c & 0x7F) << s; s += 7
            if not c & 0x80: break
        if wire == 0:
            yield num, v
        elif wire == 2:
            yield num, b[i:i + v]; i += v
        else:
            raise SystemExit(f"unexpected wire type {wire}")

n = int.from_bytes(buf[:4], "big")
header = dict(fields(buf[4:4 + n]))
assert header[1] == b"OSMHeader", header[1]
blob = dict(fields(buf[4 + n:4 + n + header[3]]))
block = dict(fields(blob[1] if 1 in blob else zlib.decompress(blob[3])))
ts = block[32]
print("writingprogram: ", block[16].decode())
print("replication ts: ", datetime.datetime.fromtimestamp(ts, datetime.UTC).isoformat())
print("replication seq:", block[33])
PY
```

Expect `osmium/1.16.0`, `2026-09-01T20:20:50+00:00`, `4897`. The published size and md5 come from
the same server:

```sh
curl -sI https://download.geofabrik.de/europe/czech-republic-260901.osm.pbf | grep -i content-length
curl -s  https://download.geofabrik.de/europe/czech-republic-260901.osm.pbf.md5
```

**The boundary relation** — note that this reads the *live* object, which is expected to move away
from version 267 over time; the derivation reads the snapshot instead:

```sh
curl -s https://api.openstreetmap.org/api/0.6/relation/442461.json \
  | python3 -c 'import json,sys; r=json.load(sys.stdin)["elements"][0]; \
      print(r["version"], r["timestamp"], r["changeset"], len(r["members"]), r["tags"]["ref:nuts"])'
```

**The generation trap**, which is the cheapest check in this file — three services, three grids:

```sh
for s in dmr5g dmr4g INSPIRE_Nadmorska_vyska; do
  printf '%-26s ' "$s"
  curl -s "https://ags.cuzk.gov.cz/arcgis2/rest/services/$s/ImageServer?f=pjson" \
    | python3 -c 'import json,sys; d=json.load(sys.stdin); print(d["pixelSizeX"], "m")'
done
```

Expect `2 m`, `5 m`, `5 m`. EL-GRID sharing DMR 4G's grid rather than DMR 5G's is the whole reason
it is rejected. The same responses carry the product descriptions the accuracy figures above were
read from — swap the print for `d["description"]` to see them.

**The ImageServer facts:**

```sh
curl -s "https://ags.cuzk.gov.cz/arcgis2/rest/services/dmr5g/ImageServer?f=pjson" \
  | python3 -c 'import json,sys; d=json.load(sys.stdin); \
      print({k: d[k] for k in ("pixelSizeX","pixelType","bandCount","maxImageWidth", \
        "maxImageHeight","exportTilesAllowed","capabilities","copyrightText")})'

curl -s "https://ags.cuzk.gov.cz/arcgis2/rest/services/dmr5g/ImageServer/query\
?where=1%3D1&outFields=Name,LowPS,HighPS&returnGeometry=false&f=pjson" \
  | python3 -c 'import json,sys; [print(f["attributes"]) for f in json.load(sys.stdin)["features"]]'
```

Expect three catalog rows — `dmr5g_raster` at LowPS 2 plus two overviews — which is half the
evidence for "single bulk render, no vintage" above. The other half is the absence of any later date
on the service:

```sh
curl -s "https://ags.cuzk.gov.cz/arcgis2/rest/services/dmr5g/ImageServer/info/metadata?f=xml" \
  | grep -oE '<CreaDate>[^<]*'
```

**What Geofabrik is still serving**, which is why the pin is a monthly:

```sh
curl -s https://download.geofabrik.de/europe/ \
  | grep -oE 'czech-republic-[0-9]{6}\.osm\.pbf' | sort -u
```

**One export window**, which checks the request shape and the nodata behaviour together:

```sh
BASE="https://ags.cuzk.gov.cz/arcgis2/rest/services/dmr5g/ImageServer/exportImage"
ARGS="bboxSR=5514&imageSR=5514&format=tiff&pixelType=F32&interpolation=RSP_NearestNeighbor&f=image"
curl -s -o /tmp/dmr5g-probe.tif "$BASE?bbox=-470000,-1100000,-469600,-1099600&size=200,200&$ARGS"
file /tmp/dmr5g-probe.tif
```

Expect `TIFF image data, little-endian, ..., height=200, bps=32, compression=none, ..., width=200`.
Terrain over that window runs 201.31–225.62 m. Repeat with `&noData=-9999` over a window crossing
the border — the `bbox` and `size` in the Nodata section, which are native 2 m — to see that table.
Both requests return `ModelPixelScale (2, 2, 0)`; if yours does not, the `size` no longer matches
the `bbox` and the server has resampled.

## Attribution

Both inputs carry obligations, settled once in [`../ATTRIBUTION.md`](../ATTRIBUTION.md): *© ČÚZK*
under CC BY 4.0 for the terrain, *© OpenStreetMap contributors* under ODbL for the geometry and for
the derived table.
