# `make check` is the gate: CI runs this file rather than a copy of the steps,
# so the two cannot drift. krpaly is three languages that arrive at different
# times, so it is one target with one step per area, and each area no-ops
# until it has files in it — one required context, one gate per area, and no
# pull request blocked on a language it did not touch. The areas run in order
# and the first failure stops the rest, so a red gate reports one area at a
# time.
.DEFAULT_GOAL := check
.PHONY: check check-derive check-db check-web format

# Tracked, or untracked and not ignored: the set of files a commit would
# carry, so a working tree and a fresh checkout agree. A directory alone means
# nothing — derive/ holds its toolchain config before it holds a line of
# Python, and that must not turn the Python steps on.
PY       := $(shell git ls-files --cached --others --exclude-standard 'derive/*.py')
SQL      := $(shell git ls-files --cached --others --exclude-standard 'db/*.sql')
WEB      := $(shell git ls-files --cached --others --exclude-standard 'web/package.json')
WEB_LOCK := $(shell git ls-files --cached --others --exclude-standard 'web/package-lock.json')

# Filtered out of PY rather than asked for separately: `git ls-files --others`
# warns on a pathspec whose directory does not exist yet, and derive/tests/ is
# the one that does not.
PY_TESTS := $(filter derive/tests/%,$(PY))

check: check-derive check-db check-web

# uv reads derive/.python-version and derive/uv.lock, fetches what it needs and
# runs the tools out of the locked environment; nothing has to be installed
# first but uv itself. --locked makes a uv.lock that has fallen behind
# pyproject.toml an error rather than a silent rewrite, so the lockfile in git
# is always the one the gate ran with.
check-derive:
ifeq ($(PY),)
	@echo "derive/: no Python yet — skipped"
else
	uv run --locked --directory derive ruff format --check .
	uv run --locked --directory derive ruff check .
# Gated separately from ruff: pytest exits 5 when it collects nothing, so a
# shared guard would fail the gate for as long as derive/ has code but no
# tests.
ifeq ($(PY_TESTS),)
	@echo "derive/: no tests yet — pytest skipped"
else
	uv run --locked --directory derive pytest
endif
endif

# No linter chosen yet, so this fails the moment there is SQL to lint rather
# than passing silently over it — a placeholder that stays quiet after its
# area lands is a gate that checks nothing.
check-db:
ifeq ($(SQL),)
	@echo "db/: no SQL yet — skipped"
else
	@echo "db/: SQL is present and unchecked — wire a linter in here"; exit 1
endif

check-web:
ifeq ($(WEB),)
	@echo "web/: no app yet — skipped"
# `npm ci` needs the lockfile and errors without it. Saying so here rather than
# letting npm say it keeps the failure about the missing commit.
else ifeq ($(WEB_LOCK),)
	@echo "web/: web/package.json without web/package-lock.json — commit the lockfile"; exit 1
else
	npm --prefix web ci
	npm --prefix web run check
endif

# The writing half of check-derive: same tools, same config, --fix instead of
# a report.
format:
ifeq ($(PY),)
	@echo "derive/: no Python yet — nothing to format"
else
	uv run --locked --directory derive ruff format .
	uv run --locked --directory derive ruff check --fix .
endif
