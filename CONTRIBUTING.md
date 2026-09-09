# Contributing

krpaly derives a database of every climb in Czechia from OSM geometry and ČÚZK
terrain. Nothing here is installed by anyone — the output is a table and the
pages built from it — so the workflow below exists for one reason: `main` stays
linear and readable, and the subject of every commit on it is deliberate rather
than whatever the last WIP commit happened to say.

## Branching

`main` is the only long-lived branch. MapyClimbs carries a `develop` because it
has a store-release train to stage; krpaly has no release train, so a second
long-lived branch would only be somewhere for `main` to drift from.

Work happens on a short-lived branch off `main`, named for the change and, when
there is one, the issue it closes:

```
<type>/<issue>-<slug>      chore/2-repo-governance
<type>/<slug>              fix/dem-cell-centres
```

Rebase onto `main` rather than merging it in — history on `main` is linear and
a merge commit cannot land.

## Commits

[Conventional Commits](https://www.conventionalcommits.org/), the same shape
climb-engine and MapyClimbs use, so `git log` reads alike across all three:

```
<type>(<scope>)!: <subject>

<body — why, not what>

Closes #12
```

**Types:** `feat`, `fix`, `perf`, `refactor`, `docs`, `test`, `build`, `ci`,
`chore`, `revert`.

**Scopes** (optional, and a closed set), grouped by area:

| Area | Scopes |
| --- | --- |
| `derive/` | `derive`, `osm`, `dem`, `engine`, `anchor` |
| `db/` | `db`, `schema` |
| `web/` | `web`, `api`, `index` |
| Everything else | `ci`, `deps`, `docs`, `test`, `build` |

A genuinely new area of the repo earns a scope by being added to `SCOPES` in
`scripts/check-pr-title.mjs` — one line of review, rather than a typo nobody
notices.

`engine` is how krpaly *calls* climb-engine: the vendored build, the harness,
the `DetectClimbsOptions` tuning. Detection itself is climb-engine's contract,
and a change to it is a pull request in that repo.

Subject: lower-case, no full stop, no trailing `(#12)` — GitHub appends the
pull request number itself — and the whole line under 72 characters.

Commits on your own branch are yours; they get squashed. The line that has to
be right is the **pull request title**, because that is the one that lands on
`main`.

## Pull requests

Every change reaches `main` through a pull request. The ruleset on `main`
requires one, requires both checks green (`check`, `pr-title`), requires review
threads resolved, forbids force-pushes, deletion and non-linear history, and
allows only the squash merge. A repository admin can bypass it; that is an
escape hatch for an emergency, not a normal Tuesday.

Squashing takes the subject from the **PR title** and the body from the **PR
description**, so both end up in `git log` verbatim. Write the description as
the thing you would want to read a year later when a climb comes out one metre
shorter than it used to: what moved, what was verified, and how.

Run the gate before pushing — CI runs exactly this:

```sh
make check
node scripts/check-pr-title.mjs "<your pull request title>"
```

The second is the `pr-title` job; the first is the `check` job, which runs this
repo's `Makefile` rather than a copy of its steps, so the two cannot drift.

`make check` needs `uv` on your `PATH` and nothing else — it fetches the Python
in `derive/.python-version` and the tools in `derive/uv.lock` itself:

```sh
curl -LsSf https://astral.sh/uv/install.sh | sh
```

krpaly is three languages that arrive at different times, so `check` is **one
job with one step per area**, and each area no-ops until it has files in it.
One required context, one gate per area: a pull request is never blocked on a
language it did not touch, and splitting it into three required contexts would
be worse — a context that never reports blocks every pull request forever, with
no error that says so. The areas run in order and the first failure stops the
rest, so a red gate reports one area at a time rather than all three.

The switch is *files*, not directories. `derive/` and `db/` both turned their
steps on this way — the first `.py` and the first `.sql` — without the workflow
being touched. `db/`'s step is `sqlfluff lint` over the migrations, run out of
`derive/`'s locked environment because that is the repo's only Python
toolchain. `web/` is the one still waiting: its step wants a
`web/package.json`, and is then `npm --prefix web ci` followed by
`npm --prefix web run check` — a `package.json` committed without its
`package-lock.json` fails loudly too, because `npm ci` needs the lockfile.

Today `make check` runs `ruff format --check`, `ruff check` and `pytest`
against `derive/`, then `sqlfluff lint` against `db/`, and skips `web/`.

`db/`'s tests are the one place `make check` needs more than `uv` on your
`PATH`: the schema tests want a real PostGIS, because every property worth
asserting there (a check constraint, a unique over an array, a GiST distance
query) is the database's behaviour rather than ours. They skip on an unset
`DATABASE_URL`, so a contributor without Postgres still gets the rest of the
gate; CI runs them against a service container and never skips.
[`db/README.md`](db/README.md) has a `docker run` recipe if you want them
locally.

**Nothing is type-checked, on purpose.** `derive/` shells out to a Node process
and reads a raster; the interesting bugs there are geometric, not type errors.
A checker earns its place when `derive/` grows a module boundary two callers
share, and that is the thing to look for rather than a line count.

`make format` is the writing half: the same tools and config with `--fix`.

Both run the tools through `uv run --locked`, so changing
`derive/pyproject.toml` without re-locking fails rather than quietly rewriting
`derive/uv.lock` under the gate. Re-lock with `uv lock --directory derive` and
commit the result.

## Test data

The derivation's real inputs are large and reproducible, so `data/`, `*.pbf`,
`*.tif` and `*.laz` are gitignored and a Geofabrik extract never enters git.
One exception, and it is what makes the pipeline testable at all: a **small
committed fixture `.pbf`**, one okres or less, cut alongside the first
extraction code rather than before it. A test suite with no committed input
tests nothing.

Every committed fixture carries a `README.md` beside it saying where its bytes
came from. A fixture *cut* from an extract is OSM data redistributed in this
repo, so **ODbL applies to it**: the README credits *© OpenStreetMap
contributors* under ODbL and names the extract, its snapshot timestamp and its
bounding box precisely enough to re-cut. A *synthetic* fixture — written by a
committed generator, as `derive/tests/fixtures/junctions.osm.pbf` is —
redistributes no OSM data and carries no ODbL obligation, and its README says
so and names the generator. Terrain fixtures, if any are ever committed, credit
*© ČÚZK* under CC BY 4.0 the same way.
