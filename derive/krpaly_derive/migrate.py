"""Apply and roll back the numbered SQL in db/migrations.

Plain SQL files with a small runner rather than Alembic: most of Alembic's
value is autogenerate, and there are no ORM models here for it to read from.
The runner lives in derive/ because derive/ is the only thing in the repo that
writes to Postgres, so one Postgres client serves both the migrator and #11's
bulk load — and because the Makefile already gates derive/*.py with ruff.

    uv run --directory derive python -m krpaly_derive.migrate up
    uv run --directory derive python -m krpaly_derive.migrate down --to 0002
"""

from __future__ import annotations

import argparse
import os
import re
import sys
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path

import psycopg

# Matches the `up` half only: discovery walks the ups and derives each down
# from its name, so a missing down is a message rather than a file silently
# absent from the listing. Four digits because the number is sorted numerically
# but printed as text, and a fixed width keeps the two orders looking alike in
# a directory listing.
UP_FILENAME = re.compile(r"^(?P<version>\d{4})_(?P<name>[a-z0-9_]+)\.up\.sql$")

# The version below the first migration: `down --to 0000` unwinds everything.
ZERO = "0000"

BOOKKEEPING = """
create table if not exists schema_migrations (
    version text primary key,
    applied_at timestamptz not null default now()
)
"""


class MigrationError(Exception):
    """A migration directory that cannot be trusted to apply in order."""


def normalise_version(value: str) -> str:
    """`2` and `0002` are the same version; `two` is not a version.

    Versions are compared as strings, which is only equivalent to comparing
    them as numbers while every one of them is four digits wide. A `--to 2`
    left unpadded compares greater than `0004` and would silently apply
    everything the flag was asking it to stop short of.
    """
    if not value.isdigit():
        raise MigrationError(f"{value!r} is not a version — expected a number like 0002")
    return f"{int(value):04d}"


@dataclass(frozen=True)
class Migration:
    version: str
    name: str
    up: Path
    down: Path


def default_directory() -> Path:
    """db/migrations, found from this file rather than from the cwd.

    The runner is invoked through `uv run --directory derive`, so the cwd is
    derive/ and never the repo root.
    """
    return Path(__file__).resolve().parents[2] / "db" / "migrations"


def discover(directory: Path | None = None) -> list[Migration]:
    """Every migration in `directory`, ordered by its number.

    Raises rather than applying a partial set: a gap, a duplicate number or an
    `up` with no `down` all mean someone's half-written pair is about to run
    against a database, and every one of them is cheap to say out loud and
    expensive to discover afterwards.
    """
    directory = directory or default_directory()
    by_version: dict[str, Migration] = {}

    for path in sorted(directory.glob("*.up.sql")):
        match = UP_FILENAME.match(path.name)
        if match is None:
            raise MigrationError(
                f"{path.name} is not NNNN_<name>.up.sql — four digits, then a lowercase name"
            )
        version, name = match["version"], match["name"]
        if version in by_version:
            raise MigrationError(
                f"two migrations numbered {version}: {by_version[version].up.name} and {path.name}"
            )
        down = path.with_name(f"{version}_{name}.down.sql")
        if not down.exists():
            raise MigrationError(f"{path.name} has no {down.name} — every up needs its down")
        by_version[version] = Migration(version=version, name=name, up=path, down=down)

    # Sorted numerically, which is the whole reason the version is parsed
    # rather than left as the filename's prefix: '0010' precedes '0002' as a
    # string, and the first symptom would be a foreign key onto a table that
    # does not exist yet.
    migrations = sorted(by_version.values(), key=lambda m: int(m.version))
    for expected, migration in enumerate(migrations, start=1):
        if int(migration.version) != expected:
            raise MigrationError(
                f"migrations jump to {migration.version} where {expected:04d} was expected — "
                "the numbering has a gap"
            )
    return migrations


def ensure_bookkeeping(conn) -> None:
    """Create schema_migrations if it is missing.

    Not a migration: it is the runner's own record of which migrations ran, so
    it cannot itself be one of them.
    """
    with conn.cursor() as cur:
        cur.execute(BOOKKEEPING)
    conn.commit()


def applied_versions(conn) -> set[str]:
    ensure_bookkeeping(conn)
    with conn.cursor() as cur:
        cur.execute("select version from schema_migrations")
        return {row[0] for row in cur.fetchall()}


def apply_up(conn, migrations: Iterable[Migration], to: str | None = None) -> list[Migration]:
    """Apply every pending migration up to and including `to`.

    Each file and its schema_migrations insert share one transaction, so a
    failure leaves neither a half-applied schema nor a version recorded for
    something that did not run.
    """
    already = applied_versions(conn)
    limit = normalise_version(to) if to is not None else None
    pending = [
        m for m in migrations if m.version not in already and (limit is None or m.version <= limit)
    ]
    for migration in pending:
        with conn.transaction(), conn.cursor() as cur:
            cur.execute(migration.up.read_text())
            cur.execute("insert into schema_migrations (version) values (%s)", (migration.version,))
        print(f"up   {migration.version}_{migration.name}", file=sys.stderr)
    return pending


def apply_down(conn, migrations: Iterable[Migration], to: str = ZERO) -> list[Migration]:
    """Roll back to `to`, exclusive — `down --to 0002` leaves 0002 applied."""
    already = applied_versions(conn)
    limit = normalise_version(to)
    unwinding = [
        m for m in reversed(list(migrations)) if m.version in already and m.version > limit
    ]
    for migration in unwinding:
        with conn.transaction(), conn.cursor() as cur:
            cur.execute(migration.down.read_text())
            cur.execute("delete from schema_migrations where version = %s", (migration.version,))
        print(f"down {migration.version}_{migration.name}", file=sys.stderr)
    return unwinding


def connect():
    url = os.environ.get("DATABASE_URL")
    if not url:
        raise SystemExit("DATABASE_URL is not set — see db/README.md for a local PostGIS")
    return psycopg.connect(url)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="krpaly_derive.migrate", description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)

    up = sub.add_parser("up", help="apply pending migrations")
    up.add_argument("--to", metavar="NNNN", help="stop after this version (default: all)")

    down = sub.add_parser("down", help="roll migrations back")
    down.add_argument(
        "--to",
        metavar="NNNN",
        default=ZERO,
        help=f"roll back to this version, exclusive (default: {ZERO}, meaning everything)",
    )

    args = parser.parse_args(argv)

    # A malformed --to or an untrustworthy migrations/ is the operator's
    # mistake, not a crash: say what is wrong on one line rather than making
    # them read a traceback for it.
    try:
        migrations = discover()
        with connect() as conn:
            if args.command == "up":
                changed = apply_up(conn, migrations, to=args.to)
            else:
                changed = apply_down(conn, migrations, to=args.to)
    except MigrationError as error:
        raise SystemExit(f"migrate: {error}") from error

    if not changed:
        print("nothing to do", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
