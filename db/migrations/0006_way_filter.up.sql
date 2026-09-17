-- #11: the predicate that decided which ways were candidates at all. The
-- boundary version and the DEM nodata value are columns for exactly this
-- reason, and the way filter moves the climb count harder than either:
-- cyclable/v1 → v2 admitted plain service roads and paved tracks (#24), and two
-- derivations that differ only by that are otherwise indistinguishable in a
-- query.
--
-- Both not null with no default, for the reason 0005 wrote out for
-- engine_config_override: a loader that forgets the column must fail rather
-- than record a plausible value by accident, and no derivation row exists
-- anywhere before this migration — #11's loader ships with it — so a not-null
-- column without a default is still safe.
alter table derivation
add column way_filter_version text not null, -- candidates.manifest.json way_filter.version
-- Filed with it rather than after it: `structure/v1` decides whether a bridge
-- or tunnel is profiled between its ends or sampled along its deck (#22), and
-- a second not-null column on a live table is a worse migration than one.
add column structure_version text not null; -- way_filter.structure
