# Attribution

Two sources, two licences, and three surfaces that have to carry them: the site, the resolution API,
and any bulk export. This file settles the credit once, at the root, because `derive/`, `db/` and
`web/` all inherit the obligation and none of them owns it.

Nothing here is a choice. Both licences are attribution-bearing, and the credit is a condition of
use rather than a courtesy.

## Terrain — © ČÚZK, CC BY 4.0

ČÚZK publishes DMR 5G as open data under [CC BY 4.0][ccby]: attribution only, no share-alike, no
non-commercial clause. It mixes into an ODbL database cleanly — see [The two licences
together](#the-two-licences-together).

Render exactly:

> Terrain © [ČÚZK](https://cuzk.gov.cz/), [CC BY 4.0][ccby] — ZABAGED® výškopis DMR 5G

The short form, for a place where one line is all there is, is `© ČÚZK` — the same string the source
service returns in its own `copyrightText` field.

## Geometry — © OpenStreetMap contributors, ODbL

OSM data is [ODbL 1.0][odbl], and [OSM's own guidance][osmcredit] fixes the wording. Render exactly:

> Geometry © [OpenStreetMap](https://www.openstreetmap.org/copyright) contributors,
> [ODbL 1.0][odbl]

**The derived table is probably a Derivative Database.** It is built by extracting OSM geometry and
sampling terrain against it, and that is the shape ODbL calls a derivative rather than a collective
work. So the share-alike condition reaches the table itself: if the climb table is ever published as
data — a bulk export, a dump, a database-shaped API — it goes out under ODbL.

**Rendered pages and profiles are Produced Works.** An elevation profile, a climb page, a map image
is a work *produced from* the database, and ODbL does not licence those: they can be licensed
freely, and need only the attribution above. This is the line that keeps the site's own content
unencumbered while the data stays open.

## The two licences together

OSM is the sole source of the share-alike obligation. CC BY 4.0 imposes attribution and nothing
else, so the terrain layer carries no condition that could conflict with ODbL — it can be mixed into
a Derivative Database without pulling the terrain into share-alike or the database out of it. Both
credits travel together; only one of them constrains what the table may be licensed as.

## Where each must appear

| Surface | What it carries |
| --- | --- |
| the site | both credits, on any page rendering geometry or elevation — footer is enough |
| the resolution API | both credits, in every response — a field on the payload, not only in docs |
| any bulk export | both credits **and** the ODbL licence text, alongside the data |
| a committed fixture | its own `README.md` beside it, per below |

"In every response" is deliberate. An API consumer never sees the site footer, and MapyClimbs is
exactly such a consumer: the credit has to arrive with the data or it does not arrive at all.

## Committed fixtures

`CONTRIBUTING.md` § "Test data" already carries this rule; it is repeated here because this is where
someone will come looking. A committed fixture `.pbf` is OSM data redistributed in this repo, so
ODbL applies to it directly: it carries a `README.md` beside it crediting *© OpenStreetMap
contributors* under ODbL and naming the extract, its snapshot timestamp and its bounding box
precisely enough to re-cut. A terrain fixture, if one is ever committed, credits *© ČÚZK* under
CC BY 4.0 the same way.

## Not a source

**Strava cannot power a social layer.** Its API terms forbid cross-user display and aggregation, so
no Strava-derived data enters this database at all and there is nothing here to attribute. Any
community data must come from GPX or FIT files uploaded directly.

## The code

The code in this repository is [MIT](LICENSE). That licence covers the code only — it says nothing
about the data, which is governed by the two licences above.

[ccby]: https://creativecommons.org/licenses/by/4.0/
[odbl]: https://opendatacommons.org/licenses/odbl/1-0/
[osmcredit]: https://osmfoundation.org/wiki/Licence/Attribution_Guidelines
