# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this
repository.

## Commands

```sh
node scripts/check-pr-title.mjs "<your pull request title>"   # the whole local gate, today
```

That is not an abbreviation — it is the complete list. `derive/`, `db/` and `web/` do not exist
yet, so there is nothing to lint, typecheck or test. #3 adds the Python toolchain (`uv`, `ruff`,
`pytest`) and a `make check` entry point that CI and a local shell run identically; until it lands,
the `check` context reports green without testing anything, and it exists only so the ruleset on
`main` has a context to require.

## What this is

A database of every climb in Czechia, derived **offline** from OSM geometry and ČÚZK terrain, and
resolved against by the [MapyClimbs](https://github.com/Kooozel/MapyClimbs) extension. Detection is
not done here: krpaly is a *consumer* of [climb-engine](https://github.com/Kooozel/climb-engine),
and a change to the detector is a pull request in that repo.

Three parts, arriving at different times:

```
derive/   Python batch: .pbf → junction-split polylines → DMR5G sampling → climb-engine → Postgres
db/       Postgres + PostGIS schema and migrations
web/      SvelteKit — SSR pages, the resolution API, the static regional anchor index
```

Derivation lives here rather than in its own repo because it shares the schema with everything else
that touches the database.

**The repo is currently `.gitignore`, `LICENSE`, `README.md`, `CONTRIBUTING.md`, `scripts/` and
`.github/`.** Everything below about `derive/`, `db/` and `web/` is settled intent, not present
code — it is written down because these are decisions that are expensive to reverse once rows
exist, not because the code is there to read.

## Constraints that bind work before it is written

These are the ones where doing it wrong means re-deriving rather than patching.

- **`way_refs` is the anchor, and it must be populated from the first extraction onward.** Identity
  is the ordered sequence of OSM way ids plus the start and end node ids — a fact about the world,
  not about the detector. The key it replaces (rounded summit + rounded gain, still live in
  `~/sport`) fails in both directions on real rows: one hill fragmenting into three identities, and
  two distinct climbs fusing at a shared summit. Retrofitting an anchor onto rows derived without
  one means re-deriving them. See #10.
- **Approach direction is part of identity, so both directions are emitted.** A climb one way is a
  descent the other, and two sides of one summit are two climbs. This is not an optimisation to
  defer — skip it in extraction and half the database is missing in a way no later stage recovers.
- **Every climb carries a `derivation_id`.** That is why a derivation is a row rather than a log
  line: a retune stays traceable and reversible, and deriving the same kraj twice creates a second
  derivation and a second set of climbs, never a conflict.
- **Pin climb-engine exactly — `v0.1.0`, never `^0.1.0`.** Detection output is the contract, so a
  floating range means the same geometry yields different climbs on a different day. Store the tag
  *and* the commit SHA (`derivation.engine_version`, `engine_commit`), read out of the release's
  `VERSION` asset rather than typed: a tag can be deleted or re-pointed, a SHA cannot. See #1.
- **The library root is the entry point, not `climb-cli`.** The CLI takes one GPX path and emits
  *ride* JSON — moving time, VAM, heart-rate zones — none of which a derived road profile has.
  `detectClimbs(tuples, …)` over a batch harness is the correct call. The pipeline diagram in §05 of
  the working spec names `climb-cli.mjs`; that line is wrong.
- **Nodata is reported, never absorbed.** A candidate crossing a hole in DEM coverage sampled as
  0 m becomes a spectacular fictional climb. Count them and decide explicitly whether they are
  dropped or interpolated across.

## Licensing, settled

- **Terrain: CC BY 4.0.** ČÚZK publishes DMR5G as open data — attribution only, no share-alike, no
  non-commercial clause. Credit *© ČÚZK* on the site, in API responses, and in any bulk export. The
  published raster is **2 m**; the quoted 0.18 m accuracy belongs to the source point cloud, and
  DMR5G is not DMR4G.
- **ODbL probably applies to the table.** A database derived from OSM geometry is likely a
  Derivative Database. Rendered pages and profiles are Produced Works and can be licensed freely.
  The CC BY terrain layer mixes in cleanly — OSM is the sole source of the obligation.
- **Strava cannot power a social layer.** Its API terms forbid cross-user display and aggregation.
  Any community data must come from GPX/FIT uploaded directly.

## Data, and what is never committed

Derivation inputs are large and reproducible: `data/`, `*.pbf`, `*.tif`, `*.tiff`, `*.laz` are all
gitignored, and a multi-GB Geofabrik extract or a kraj of DMR5G tiles never enters git. What *is*
committed is the code, a **manifest** naming each input precisely enough to re-obtain it (snapshot
timestamp and sha256, boundary relation id and version, DEM tile set and CRS as the files actually
declare it), and — from #3 onward — one small fixture `.pbf` that makes the pipeline testable at
all. A test suite with no committed input tests nothing.

Heart-rate zones and anything else personal are never committed.

## Workflow

`CONTRIBUTING.md` is the long form; what matters when working here:

- **`main` is the only long-lived branch.** MapyClimbs carries a `develop` because it stages a
  store-release train; krpaly has no release train, so a second long-lived branch would only be
  somewhere for `main` to drift from. Work on `<type>/<issue>-<slug>` off `main`
  (`chore/2-repo-governance`), and rebase rather than merging `main` in — history is linear and a
  merge commit cannot land.
- **Every change goes through a pull request.** The ruleset on `main` requires one, requires both
  checks green (`check`, `pr-title`), requires review threads resolved, forbids force-push,
  deletion and non-linear history, and allows only the squash merge. An admin bypass exists for
  emergencies; do not reach for it.
- **Conventional Commits**, `<type>(<scope>)!: <subject>`, the same shape climb-engine and
  MapyClimbs use. Types: `feat` `fix` `perf` `refactor` `docs` `test` `build` `ci` `chore`
  `revert`. Scopes are a closed set, grouped by area — `derive` `osm` `dem` `engine` `anchor` /
  `db` `schema` / `web` `api` `index` / `ci` `deps` `docs` `test` `build` — and **a new area earns
  one by being added to `SCOPES` in `scripts/check-pr-title.mjs`**, which is a line of review rather
  than a typo nobody notices. `engine` there means how krpaly *calls* climb-engine — the vendored
  build, the harness, `DetectClimbsOptions` tuning — never detection itself.
- **The pull request title is the commit message on `main`.** Squashing takes the subject from the
  title and the body from the description, so both land in `git log` verbatim. Branch commits are
  squashed and are yours.
- Close issues from the description (`Closes #12`), not from a commit trailer — the squashed body is
  the description anyway, and the link then survives a retitle.

## Agent skills

Configuration the [engineering skills](https://github.com/mattpocock/skills) read before they act.

### Issue tracker

GitHub Issues on `Kooozel/krpaly`, via the `gh` CLI. See `docs/agents/issue-tracker.md`.

### Triage labels

The five canonical roles, each label string equal to its name. See `docs/agents/triage-labels.md`.

### Domain docs

Single-context — one `CONTEXT.md` and `docs/adr/` at the root, both created lazily by
`/domain-modeling` rather than upfront. See `docs/agents/domain.md`.
