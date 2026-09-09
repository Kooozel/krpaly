-- A derivation is a row rather than a log line so that a retune is traceable
-- and reversible: every climb carries derivation_id, and deriving the same
-- kraj twice creates a second derivation and a second set of climbs rather
-- than a conflict.
--
-- The provenance columns are explicit scalars rather than an osm_snapshot and
-- a dem_source blob, per derive/INPUTS.md § "Provenance columns for #5" — a
-- wrong value should be visible in a column, and queryable, rather than buried
-- in JSON.
--
-- There is no region_code here on purpose. Climbs are assigned to a kraj by
-- their summit, so a derivation of Moravskoslezský legitimately produces
-- climbs in Zlínský. The kraj a derivation *ran over* is boundary_relation_id;
-- the kraj a climb *belongs to* is on the climb.
create table derivation (
    id bigint generated always as identity primary key,
    created_at timestamptz not null default now(),

    -- #1: the tag is what a human reads, the SHA is what survives a tag being
    -- deleted or re-pointed. Both are read out of the release's VERSION asset.
    engine_version text not null,
    engine_commit text not null,
    -- detectClimbs returns MeasuredClimb, which carries no category and no
    -- difficulty — "is this a climb" is the consumer's question. A scoring
    -- model answers it, and the three shipped models disagree substantially.
    -- Without this column climb.category and climb.difficulty are not
    -- comparable across derivations.
    scoring_model text not null,

    osm_snapshot_url text not null,
    -- Ours, not Geofabrik's: they publish md5. #6 computes the sha256 of the
    -- bytes it downloaded, the published md5 proving the transfer was clean
    -- and the sha256 identifying the file afterwards.
    osm_snapshot_sha256 text not null,
    osm_snapshot_replication_ts timestamptz not null,
    osm_snapshot_seq bigint not null,

    boundary_relation_id bigint not null,
    -- The version found in the extract, never the live one: the derivation
    -- reads the snapshot, and a boundary that moved is one of the failure
    -- modes this milestone exists to surface.
    boundary_relation_version integer not null,
    boundary_buffer_m integer not null,
    boundary_assignment text not null,

    dem_product text not null,
    dem_route text not null,
    dem_resolution_m numeric not null,
    dem_crs integer not null, -- 5514, S-JTSK / Krovák East North
    dem_vertical_crs integer not null, -- 8357, Bpv
    -- -9999. A row derived before this parameter was passed is not comparable
    -- to one derived after: without it uncovered pixels arrive as 0.0 with
    -- nothing in the file to say so.
    dem_nodata_value real not null,
    -- Per-window checksums live in #7's committed manifest; the row points at
    -- it rather than carrying it.
    dem_manifest_sha256 text not null,
    dem_fetched_at timestamptz not null,

    constraint derivation_engine_commit_is_sha
    check (engine_commit ~ '^[0-9a-f]{40}$'),
    constraint derivation_scoring_model_known
    check (scoring_model in ('aso', 'garmin', 'hiking')),
    -- A single permitted value, so that a change of border policy is a
    -- migration rather than a silent new string in the data.
    constraint derivation_boundary_assignment_known
    check (boundary_assignment in ('summit'))
);
