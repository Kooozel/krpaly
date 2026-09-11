-- #9: a climb is a function of the detector *and* its configuration, so the
-- configuration is provenance as much as engine_commit is. The tag and SHA say
-- which build ran; this says what it was told.
--
-- One jsonb rather than a scalar per key, unlike 0003's provenance columns.
-- Those are a fixed set of facts about the inputs; this is an open-ended set of
-- climb-engine's DetectClimbsOptions keys, which move with the engine version,
-- and a column per key would be a migration per retune. It holds only the
-- override — what krpaly changed from the pinned build's defaults — which with
-- engine_commit is enough to reproduce the effective configuration. An empty
-- object is the defaults, and is the common case.
--
-- No default, deliberately: a loader that forgets the column must fail rather
-- than record "no override" by accident. No derivation row exists anywhere yet,
-- so adding a not-null column without one is safe.
alter table derivation
add column engine_config_override jsonb not null,
-- An object, because the engine merges it over its defaults as one. 'null' is
-- a jsonb value, so `not null` alone would let it through.
add constraint derivation_engine_config_override_is_object
check (jsonb_typeof(engine_config_override) = 'object');
