-- Both columns go together: they are one fact about the extraction, filed as
-- two because the predicate and the structure rule version independently.
alter table derivation
drop column way_filter_version,
drop column structure_version;
