# `db/` — the schema, and how to run it

Postgres 17 with PostGIS 3.5. Four tables, applied by numbered SQL through a runner that lives in
[`derive/`](../derive). #6–#9 write into these tables; this file is what they read first.

Only the part of §06 the first derivation needs is here. `app_user`, `oauth_account`, `ascent` and
`climb_stats` are designed in the spec and deliberately not created: v1 is climb pages, region
browse and the static index, and an empty users table invites a foreign key and then a migration to
remove it.

## Running it

The runner reads `DATABASE_URL` and finds `db/migrations/` from its own location, so it does not
care what the working directory is.

```sh
uv run --directory derive python -m krpaly_derive.migrate up             # apply everything pending
uv run --directory derive python -m krpaly_derive.migrate up --to 0002   # stop after 0002
uv run --directory derive python -m krpaly_derive.migrate down --to 0002 # roll back to 0002, which stays applied
uv run --directory derive python -m krpaly_derive.migrate down --to 0000 # unwind everything
```

`--to` is inclusive going up and exclusive coming down, which is the reading that makes
`up --to N` and `down --to N` land on the same schema. It is zero-padded for you, so `--to 2` and
`--to 0002` are the same request.

Each migration and its `schema_migrations` insert share one transaction, so a failure leaves neither
a half-applied file nor a version recorded for something that did not run. `schema_migrations` is
the runner's own bookkeeping and is created by the runner, not by a migration — it cannot be one of
the things it records.

Discovery refuses to run at all on a directory it cannot trust: a gap in the numbering, two files
with the same number, or an `up` with no `down`. A half-written pair should fail before it touches a
database rather than after.

### A local PostGIS

```sh
docker run --rm -d -p 5432:5432 -e POSTGRES_PASSWORD=postgres --name krpaly-pg postgis/postgis:17-3.5

# Wait for the real server. The image's initdb brings up a temporary one first,
# and `pg_isready` answers for that one too — connect on the way past it and
# the server closes the connection mid-request.
until docker exec krpaly-pg psql -U postgres -c 'select 1' >/dev/null 2>&1; do sleep 1; done

export DATABASE_URL=postgresql://postgres:postgres@localhost:5432/postgres
uv run --directory derive python -m krpaly_derive.migrate up
make check          # the schema tests now run instead of skipping
docker rm -f krpaly-pg
```

`down --to 0000` leaves `schema_migrations` and PostGIS's own `spatial_ref_sys` behind, and nothing
else — the first is the runner's bookkeeping and the second belongs to the extension.

Without `DATABASE_URL` the schema tests skip and everything else in `make check` still runs — the
SQL lint included. CI always sets it, so CI never skips.

## Migrations

| File | What it does |
| --- | --- |
| `0001_postgis` | `create extension if not exists postgis` |
| `0002_region` | `region` and its fourteen-row seed |
| `0003_derivation` | `derivation` and its provenance scalars |
| `0004_climb` | `climb`, `climb_profile`, four indexes |

`0001`'s `down` is deliberately empty. Its `up` is `create extension if not exists`, which is a
no-op on every PostGIS image — the extension is already there — so dropping it would remove
something the migration did not create. It would also fail: `postgis_topology` and
`postgis_tiger_geocoder` depend on `postgis`, and the `cascade` that got past that would take both
with it.

## `region`

Fourteen immutable rows, seeded from the OSM relations and read from Overpass on 2026-09-09. The
key is natural — `code` is `ref:nuts` — rather than a surrogate id: the codes never change, so an id
buys nothing, and region browse and the static regional index read the region straight off a climb
row with no join.

| Column | Meaning |
| --- | --- |
| `code` | `ref:nuts`, e.g. `CZ080`. Primary key. |
| `name` | OSM's `name` tag verbatim, so Praha is `Praha` and not `Hlavní město Praha`. |
| `slug` | URL segment, diacritic-free. Unique. |
| `osm_relation_id` | The `admin_level=4` relation. Unique. CZ080 is 442461, which is the relation `derive/INPUTS.md` pins for kraj-1. |

## `derivation`

A derivation is a row rather than a log line so that a retune is traceable and reversible. Every
climb carries `derivation_id`, and deriving the same kraj twice creates a second derivation and a
second set of climbs rather than a conflict.

The provenance is sixteen explicit scalars rather than an `osm_snapshot` and a `dem_source` blob,
per `derive/INPUTS.md` § *Provenance columns for #5*: a wrong value should be visible in a column,
and queryable, rather than buried in JSON.

| Column | Meaning |
| --- | --- |
| `engine_version` | climb-engine's tag, e.g. `v0.1.0`. What a human reads. |
| `engine_commit` | Its commit SHA. What survives a tag being deleted or re-pointed. Both are read out of the release's `VERSION` asset rather than typed. Constrained to 40 lowercase hex. |
| `scoring_model` | `aso`, `garmin` or `hiking`. See [Why there is a scoring model](#why-there-is-a-scoring-model). |
| `osm_snapshot_url` | The Geofabrik monthly archive. `-latest` and the dated dailies are both unpinnable; only `YYMM01` is still there next year. |
| `osm_snapshot_sha256` | Ours, not Geofabrik's — they publish md5. #6 computes it over the bytes it downloaded. |
| `osm_snapshot_replication_ts` | `osmosis_replication_timestamp` from the `.pbf` header. The fact about the data, as opposed to the filename. |
| `osm_snapshot_seq` | `osmosis_replication_sequence_number`. |
| `boundary_relation_id` | The kraj the derivation *ran over*. |
| `boundary_relation_version` | **The version found in the extract, never the live one.** The derivation reads the snapshot, and a boundary that moved under it is one of the failure modes this milestone exists to surface. |
| `boundary_buffer_m` | 10000 today. Extract with a buffer, detect over the buffered geometry, assign by summit — so nothing is truncated. |
| `boundary_assignment` | `summit`, and constrained to exactly that, so a change of policy is a migration rather than a silent new string. |
| `dem_product` | ZABAGED® – Výškopis – DMR 5G, obchodní kód 64111. |
| `dem_route` | The ImageServer endpoint. DMR 5G publishes no raster; 2 m is ČÚZK's mosaic, a service derived from the LAZ point cloud. |
| `dem_resolution_m` | 2. |
| `dem_crs` | 5514 — S-JTSK / Krovák East North. |
| `dem_vertical_crs` | 8357 — Bpv. |
| `dem_nodata_value` | −9999. Exists because a row derived before that parameter was passed is **not comparable** to one derived after: without it, uncovered pixels arrive as `0.0` with nothing in the file to say so, and a candidate crossing them becomes a spectacular fictional climb. |
| `dem_manifest_sha256` | Points at #7's committed per-window manifest rather than carrying it. |
| `dem_fetched_at` | When. This route publishes no vintage, so this is the only date there is. |

There is **no `region_code` on `derivation`**, on purpose. Climbs are assigned to a kraj by their
summit, so a derivation of Moravskoslezský legitimately produces climbs in Zlínský. The kraj a
derivation ran over is `boundary_relation_id`; the kraj a climb belongs to is on the climb.

### Why there is a scoring model

`detectClimbs` returns `MeasuredClimb`, which carries neither a category nor a difficulty — "is this
a climb" is the consumer's question, not the detector's. A scoring model answers it, and the three
shipped models disagree substantially: over one route's eight candidates, ASO keeps three, Garmin
five, hiking one. So `climb.category` and `climb.difficulty` are meaningless without knowing which
model produced them, and the column sits on `derivation` in the same spirit as `engine_version`.

## `climb`

| Column | Meaning |
| --- | --- |
| `derivation_id` | `on delete cascade`. Reverting a retune leaves nothing behind to be joined against. |
| `region_code` | By the position of the summit. |
| `way_refs` | **The anchor.** The ordered sequence of OSM way ids. |
| `start_node_id`, `end_node_id` | The other half of the anchor. |
| `slug`, `name` | **Nullable on purpose.** The algorithm produces geometry, never identity; the schema should not pretend a name exists. |
| `start_pt` | `MeasuredClimb.markerCoords`. |
| `top_pt` | `MeasuredClimb.endCoords`, snapped. |
| `dist_m` | `MeasuredClimb.distance`. |
| `gain_m` | `MeasuredClimb.elevation`. |
| `avg_grade` | Per cent, as the engine gives it. |
| `max_grade` | Per cent. **The loader converts** — see below. |
| `difficulty`, `category` | Both nullable, and null is data: it means the derivation's scoring model cleared no threshold for this climb. `uncategorized` is a category, not an absence. |

### The two conversions the loader owes

1. **`max_grade` is `maxSustainedGradient × 100`.** The engine reports `avgGrade` in per cent and
   `maxSustainedGradient` as a decimal fraction — `0.25` is 25 %. Two grade columns side by side in
   one row have to agree on their unit, and this schema stores both as per cent.
2. **`way_refs` is ordered, and the order is the direction of travel.** Approach direction is part
   of identity: a climb one way is a descent the other, and two sides of one summit are two climbs.
   Both directions are emitted, and they are two rows with two anchors, not one row with a flag.

### The anchor

Identity is the ordered way sequence plus the start and end node ids — a fact about the world rather
than about the detector, so a re-derived climb maps deterministically onto its predecessor. The key
it replaces, a rounded summit plus a rounded gain, fails in both directions on real rows: one hill
fragmenting into three identities, and two distinct climbs fusing at a shared summit.

`climb_anchor_unique` is `(derivation_id, way_refs, start_node_id, end_node_id)` — unique **within a
derivation, not globally**, because deriving the same kraj twice must produce a second set of climbs
rather than a conflict. #10's dedupe leans on exactly this.

It is a btree index over an array, which has a size ceiling: measured against this schema, the
largest `way_refs` it accepts is **473** incompressible way ids, refusing the 474th with *index row
size 2712 exceeds btree version 4 maximum 2704*. Junction-split polylines put a real climb orders of
magnitude below that, so this is a bound to know about rather than one to design around. If #6 ever
approaches it, the fix is a generated digest column carrying the uniqueness, not a wider index.

`climb_slug_unique` is `(derivation_id, slug)`, and it is **not a NOT NULL by another name**:
Postgres treats nulls as distinct, so every unnamed climb in a derivation coexists happily. Until
naming lands, that is all of them.

## `climb_profile`

One row per climb: the geometry and one elevation per vertex.

`climb_profile_elevations_match_geom` checks
`coalesce(array_length(elevations, 1), 0) = st_npoints(geom::geometry)`. The `coalesce` is
load-bearing rather than decorative — `array_length` of an empty array is `null`, a `CHECK` that
evaluates to `null` passes, and `elevations = '{}'` would otherwise sail through the one constraint
written to catch it.

`st_npoints` and the geography-to-geometry cast are both immutable, so PostgreSQL accepts them
inside a `CHECK`. Verified against PostGIS 3.5 rather than assumed.

## Why `geography(…, 4326)` and not `geometry`

The resolution API's question is "top within ~150 m, start within ~250 m". On `geography`,
`ST_DWithin(top_pt, $1, 150)` takes metres directly and is GiST-indexed.

The alternative that is genuinely faster — `geometry` in EPSG:5514 — is metric only after
reprojecting both ends. OSM geometry arrives in WGS-84, `MeasuredClimb`'s coordinates are WGS-84,
and the extension asks in WGS-84 and wants GeoJSON back. That is two `ST_Transform`s on every read
to save microseconds on a table of low tens of thousands of rows. `geography` also makes
`ST_Length(climb_profile.geom)` metres for free. The point columns and the profile line agree, which
was the constraint worth keeping either way.

## Indexes

```sql
create index climb_slug_idx on climb (slug) where slug is not null;
create index climb_region_idx on climb (region_code);
create index climb_top_pt_idx on climb using gist (top_pt);
create index climb_start_pt_idx on climb using gist (start_pt);
```

The first two are the two access paths that exist — a climb by slug, and climbs within a region. The
GiST pair is what the resolution API needs; they cost nothing on a table this size and adding them
later is an index build on a live table. There is no index on `derivation_id` alone, because
`climb_anchor_unique` already leads with it.

## Linting

`make check` runs `sqlfluff lint` over this directory out of `derive/`'s locked environment —
`derive/` is the repo's only Python toolchain, and a second lockfile for one linter is worse than
one misfiled dependency. `make format` runs `sqlfluff fix`. The config is [`.sqlfluff`](.sqlfluff),
which sqlfluff finds by searching upward from each file; the templater is the one setting it refuses
to read from a subdirectory, so the `Makefile` passes `--templater raw` explicitly.
