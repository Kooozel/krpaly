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

Run the gate before pushing. Right now that is one command, because `derive/`,
`db/` and `web/` do not exist yet:

```sh
node scripts/check-pr-title.mjs "<your pull request title>"
```

CI runs that as the `pr-title` job. The other required context, `check`, is
deliberately empty — it exists so the ruleset has something to require, and it
reports green without testing anything. #3 gives it a body: the Python
toolchain, `make check`, and a step per area that no-ops while that area is
absent. When that lands, this block grows to match it.
