-- geography(…, 4326) rather than geometry in a projected CRS. The resolution
-- API's question is "top within ~150 m, start within ~250 m", and
-- ST_DWithin(top_pt, $1, 150) on geography takes metres directly and is
-- GiST-indexed. geometry in EPSG:5514 is metric only after reprojecting both
-- ends: OSM geometry arrives in WGS-84, MeasuredClimb's coordinates are
-- WGS-84, and the extension asks in WGS-84 and wants GeoJSON back. That is two
-- ST_Transforms on every read to save microseconds on a table of low tens of
-- thousands of rows. geography also makes ST_Length(climb_profile.geom) metres
-- for free.
create table climb (
    id bigint generated always as identity primary key,
    derivation_id bigint not null references derivation (id) on delete cascade,
    -- By the summit's position, per derive/INPUTS.md's border policy: nothing
    -- is truncated and each climb lands in exactly one kraj no matter how many
    -- derivations see it.
    region_code text not null references region (code),

    -- The anchor: the ordered way sequence plus the start and end node. A fact
    -- about the world rather than about the detector, so a re-derived climb
    -- maps deterministically onto its predecessor. Populated from the first
    -- extraction onward — retrofitting it onto rows derived without it means
    -- re-deriving them.
    way_refs bigint[] not null,
    start_node_id bigint not null,
    end_node_id bigint not null,

    -- Nullable on purpose. The algorithm produces geometry, never identity,
    -- and the schema should not pretend a name exists.
    slug text,
    name text,

    start_pt geography (point, 4326) not null, -- MeasuredClimb.markerCoords
    top_pt geography (point, 4326) not null, -- MeasuredClimb.endCoords, snapped

    dist_m real not null, -- MeasuredClimb.distance
    gain_m real not null, -- MeasuredClimb.elevation
    avg_grade real not null, -- per cent, as the engine gives it
    -- per cent — maxSustainedGradient × 100, which the engine reports as a
    -- decimal fraction. The loader owes this conversion: two grade columns
    -- side by side in one row must agree on their unit.
    max_grade real not null,
    -- Null is data in both: it means the scoring model named on the derivation
    -- cleared no threshold for this climb. 'uncategorized' is a category, not
    -- an absence.
    difficulty double precision,
    category text,

    constraint climb_way_refs_nonempty check (cardinality(way_refs) > 0),
    constraint climb_dist_positive check (dist_m > 0),
    constraint climb_gain_positive check (gain_m > 0),
    constraint climb_category_known
    check (category is null or category in ('HC', '1', '2', '3', '4', 'uncategorized')),

    -- Deriving the same kraj twice creates a second derivation and a second
    -- set of climbs, never a conflict — so the anchor is unique within a
    -- derivation, not globally. #10's dedupe leans on this.
    constraint climb_anchor_unique
    unique (derivation_id, way_refs, start_node_id, end_node_id),
    constraint climb_slug_unique unique (derivation_id, slug)
);

create table climb_profile (
    climb_id bigint primary key references climb (id) on delete cascade,
    geom geography (linestring, 4326) not null,
    elevations real[] not null,
    -- The classic off-by-one: one elevation per vertex, or the profile is
    -- drawn against the wrong geometry with nothing to say so. The coalesce is
    -- load-bearing — array_length of an empty array is null, and a null CHECK
    -- passes, which would let elevations = '{}' through the one constraint
    -- meant to catch it.
    constraint climb_profile_elevations_match_geom
    check (coalesce(array_length(elevations, 1), 0) = st_npoints(geom::geometry))
);

-- Partial, because most rows have no slug until something names them.
create index climb_slug_idx on climb (slug) where slug is not null;
create index climb_region_idx on climb (region_code);
-- The resolution API's two spatial questions. Cheap on a table this size, and
-- adding them later means an index build on a live table.
create index climb_top_pt_idx on climb using gist (top_pt);
create index climb_start_pt_idx on climb using gist (start_pt);

-- No index on derivation_id alone: climb_anchor_unique already leads with it.
