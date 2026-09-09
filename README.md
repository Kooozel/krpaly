# krpaly

A database of every climb in Czechia, derived offline from OpenStreetMap
geometry and ČÚZK terrain, and resolved against it by the
[MapyClimbs](https://github.com/Kooozel/MapyClimbs) extension.

## The three parts

| Part | What it is |
| --- | --- |
| `derive/` | Python batch: OSM `.pbf` → junction-split polylines → DMR5G elevation sampling → [climb-engine](https://github.com/Kooozel/climb-engine) → Postgres |
| `db/` | Postgres + PostGIS schema and migrations |
| `web/` | SvelteKit app — SSR pages, the resolution API, and the static regional anchor index |

Derivation lives here rather than in its own repo because it shares the schema
with everything else that touches the database.

## Settled constraints

- **Terrain: CC BY 4.0.** ČÚZK publishes DMR 5G as open data — attribution
  only, no share-alike, no non-commercial clause. It ships as LAZ point data in
  a TIN, not as a raster; the 2 m grid is ČÚZK's ImageServer mosaic derived from
  it, and the accuracy that matters here is **0,3 m under forest** rather than
  the 0,18 m quoted for open terrain. `derive/INPUTS.md` pins the source and
  `ATTRIBUTION.md` carries the credit strings.
- **ODbL likely applies to the table.** A database derived from OSM geometry is
  probably a Derivative Database. Rendered pages and profiles are Produced Works
  and can be licensed freely. The CC BY terrain layer mixes in cleanly — OSM is
  the sole source of the obligation. See `ATTRIBUTION.md`.
- **Strava cannot power a social layer.** Its API terms forbid cross-user
  display and aggregation. Any community data must come from GPX/FIT files
  uploaded directly.

## Open questions

- **Anchoring.** Rounded-summit keys both fragment one climb into several
  identities and risk fusing distinct climbs that share a summit — proven
  against real rows in `~/sport/garmin.db`. OSM way-sequence anchoring is the
  intended fix and is unvalidated until one okres is derived.
- **Scale.** The climb count and the derivation's wall-clock and disk cost are
  outputs, not inputs. Low tens of thousands is the working assumption.

## Status

Governance and the Python toolchain are in place — `make check` is the gate,
and it grows a step as each of the three areas lands. The derivation's inputs
are pinned in `derive/INPUTS.md`. Nothing is derived yet.
