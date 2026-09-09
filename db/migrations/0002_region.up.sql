-- Fourteen immutable rows. `code` is the natural key (ref:nuts) rather than a
-- surrogate id: the codes never change, so a surrogate buys nothing, and
-- region browse and the static regional index read the region straight off a
-- climb row with no join.
create table region (
    code text primary key, -- ref:nuts, e.g. 'CZ080'
    name text not null, -- OSM's `name` tag verbatim
    slug text not null unique, -- URL segment, diacritic-free
    osm_relation_id bigint not null unique
);

-- Seeded from the OSM relations, read from Overpass on 2026-09-09. CZ080 →
-- 442461 is the same relation derive/INPUTS.md pins for kraj-1. `name` is the
-- OSM tag as it stands, so Praha is 'Praha' and not 'Hlavní město Praha'.
insert into region (code, name, slug, osm_relation_id) values
('CZ010', 'Praha', 'praha', 435514),
('CZ020', 'Středočeský kraj', 'stredocesky', 442397),
('CZ031', 'Jihočeský kraj', 'jihocesky', 442321),
('CZ032', 'Plzeňský kraj', 'plzensky', 442466),
('CZ041', 'Karlovarský kraj', 'karlovarsky', 442314),
('CZ042', 'Ústecký kraj', 'ustecky', 442452),
('CZ051', 'Liberecký kraj', 'liberecky', 442455),
('CZ052', 'Královéhradecký kraj', 'kralovehradecky', 442463),
('CZ053', 'Pardubický kraj', 'pardubicky', 442460),
('CZ063', 'Kraj Vysočina', 'vysocina', 442453),
('CZ064', 'Jihomoravský kraj', 'jihomoravsky', 442311),
('CZ071', 'Olomoucký kraj', 'olomoucky', 442459),
('CZ072', 'Zlínský kraj', 'zlinsky', 442449),
('CZ080', 'Moravskoslezský kraj', 'moravskoslezsky', 442461);
