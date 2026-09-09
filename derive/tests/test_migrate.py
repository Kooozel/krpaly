"""The migration runner, and the schema it applies.

Split in two. The discovery half is a pure function over a directory listing
and runs anywhere. The rest needs a real PostGIS — mocking a database would
test the mock, and every property worth asserting here (a check constraint, a
unique over an array, a GiST distance query) is the database's behaviour rather
than ours. Those carry a skipif on DATABASE_URL: CI always sets it, so CI never
skips; the skip only spares a contributor without Postgres, who still gets the
discovery tests and the SQL lint.
"""

import os
from pathlib import Path

import pytest

from krpaly_derive.migrate import (
    MigrationError,
    applied_versions,
    apply_down,
    apply_up,
    discover,
    ensure_bookkeeping,
)

REQUIRES_DB = pytest.mark.skipif(
    os.environ.get("DATABASE_URL") is None,
    reason="needs a PostGIS database; set DATABASE_URL",
)

# The three pinned inputs from #4, as derive/INPUTS.md records them. The two
# digests are placeholders: #6 computes the snapshot's sha256 at fetch and #7
# writes the DEM manifest, so neither exists yet.
PINNED_DERIVATION = {
    "engine_version": "v0.1.0",
    "engine_commit": "0" * 40,
    "scoring_model": "aso",
    "osm_snapshot_url": "https://download.geofabrik.de/europe/czech-republic-260901.osm.pbf",
    "osm_snapshot_sha256": "a" * 64,
    "osm_snapshot_replication_ts": "2026-09-01T20:20:50Z",
    "osm_snapshot_seq": 4897,
    "boundary_relation_id": 442461,
    "boundary_relation_version": 267,
    "boundary_buffer_m": 10000,
    "boundary_assignment": "summit",
    "dem_product": "ZABAGED® – Výškopis – DMR 5G, obchodní kód 64111",
    "dem_route": "https://ags.cuzk.gov.cz/arcgis2/rest/services/dmr5g/ImageServer",
    "dem_resolution_m": 2,
    "dem_crs": 5514,
    "dem_vertical_crs": 8357,
    "dem_nodata_value": -9999,
    "dem_manifest_sha256": "b" * 64,
    "dem_fetched_at": "2026-09-09T00:00:00Z",
}


def write_pair(directory: Path, stem: str, *, down: bool = True) -> None:
    (directory / f"{stem}.up.sql").write_text("select 1;\n")
    if down:
        (directory / f"{stem}.down.sql").write_text("select 1;\n")


# --- discovery, no database ------------------------------------------------


def test_discovers_the_repos_own_migrations():
    migrations = discover()
    assert [m.version for m in migrations] == ["0001", "0002", "0003", "0004"]
    assert migrations[0].name == "postgis"
    assert migrations[-1].name == "climb"


def test_sorts_by_numeric_prefix_not_lexically(tmp_path):
    # The one that bites at ten migrations: lexically '0010' sorts between
    # '0001' and '0002', and the first symptom would be a table created before
    # the table it references. Ten is the smallest set that tells the two
    # orders apart while staying contiguous.
    for number in range(10, 0, -1):
        write_pair(tmp_path, f"{number:04d}_m{number}")
    assert [m.version for m in discover(tmp_path)] == [f"{n:04d}" for n in range(1, 11)]


def test_a_gap_in_the_numbering_raises(tmp_path):
    write_pair(tmp_path, "0001_a")
    write_pair(tmp_path, "0003_c")
    with pytest.raises(MigrationError, match="0002"):
        discover(tmp_path)


def test_a_duplicate_number_raises(tmp_path):
    write_pair(tmp_path, "0001_a")
    write_pair(tmp_path, "0001_b")
    with pytest.raises(MigrationError, match="0001"):
        discover(tmp_path)


def test_an_up_without_its_down_raises(tmp_path):
    write_pair(tmp_path, "0001_a")
    write_pair(tmp_path, "0002_b", down=False)
    with pytest.raises(MigrationError, match="0002_b.down.sql"):
        discover(tmp_path)


def test_an_empty_directory_is_not_an_error(tmp_path):
    assert discover(tmp_path) == []


# --- the database ----------------------------------------------------------


@pytest.fixture
def conn():
    import psycopg

    with psycopg.connect(os.environ["DATABASE_URL"]) as connection:
        with connection.cursor() as cur:
            cur.execute(
                "drop table if exists climb_profile, climb, derivation, region, "
                "schema_migrations cascade"
            )
        connection.commit()
        yield connection


@pytest.fixture
def migrated(conn):
    apply_up(conn, discover())
    return conn


def table_names(conn) -> set[str]:
    with conn.cursor() as cur:
        cur.execute("select tablename from pg_tables where schemaname = 'public'")
        return {row[0] for row in cur.fetchall()}


def insert_derivation(conn, **overrides) -> int:
    values = PINNED_DERIVATION | overrides
    columns = ", ".join(values)
    placeholders = ", ".join(f"%({k})s" for k in values)
    with conn.cursor() as cur:
        cur.execute(
            f"insert into derivation ({columns}) values ({placeholders}) returning id",
            values,
        )
        return cur.fetchone()[0]


def insert_climb(conn, derivation_id: int, **overrides) -> int:
    values = {
        "derivation_id": derivation_id,
        "region_code": "CZ080",
        "way_refs": [1, 2, 3],
        "start_node_id": 10,
        "end_node_id": 20,
        "start_pt": "SRID=4326;POINT(18.29 49.83)",
        "top_pt": "SRID=4326;POINT(18.30 49.84)",
        "dist_m": 1000.0,
        "gain_m": 80.0,
        "avg_grade": 8.0,
        "max_grade": 12.5,
    } | overrides
    columns = ", ".join(values)
    placeholders = ", ".join(f"%({k})s" for k in values)
    with conn.cursor() as cur:
        cur.execute(
            f"insert into climb ({columns}) values ({placeholders}) returning id",
            values,
        )
        return cur.fetchone()[0]


@REQUIRES_DB
def test_up_creates_the_schema_on_a_clean_database(migrated):
    # The ticket's first completion criterion.
    assert {"region", "derivation", "climb", "climb_profile"} <= table_names(migrated)
    assert applied_versions(migrated) == {"0001", "0002", "0003", "0004"}


@REQUIRES_DB
def test_region_is_seeded_with_the_fourteen_kraje(migrated):
    with migrated.cursor() as cur:
        cur.execute("select count(*) from region")
        assert cur.fetchone()[0] == 14
        cur.execute("select osm_relation_id from region where code = 'CZ080'")
        assert cur.fetchone()[0] == 442461


@REQUIRES_DB
def test_the_round_trip_is_repeatable_rather_than_one_way(migrated):
    # The ticket's "and rolls back", plus the half that makes it worth having:
    # that `up` works again afterwards.
    apply_down(migrated, discover(), to="0000")
    assert not {"region", "derivation", "climb", "climb_profile"} & table_names(migrated)
    assert applied_versions(migrated) == set()

    apply_up(migrated, discover())
    assert {"region", "derivation", "climb", "climb_profile"} <= table_names(migrated)


@REQUIRES_DB
def test_down_to_a_version_stops_there(migrated):
    apply_down(migrated, discover(), to="0002")
    assert applied_versions(migrated) == {"0001", "0002"}
    assert "region" in table_names(migrated)
    assert "climb" not in table_names(migrated)


@REQUIRES_DB
def test_up_is_idempotent_when_everything_is_applied(migrated):
    apply_up(migrated, discover())
    assert applied_versions(migrated) == {"0001", "0002", "0003", "0004"}


@REQUIRES_DB
def test_a_failing_migration_records_no_version(conn, tmp_path):
    # The reason the insert shares the migration's transaction: a half-applied
    # version in schema_migrations is worse than no version at all.
    (tmp_path / "0001_broken.up.sql").write_text("create table t (); selct 1;\n")
    (tmp_path / "0001_broken.down.sql").write_text("drop table t;\n")
    ensure_bookkeeping(conn)
    with pytest.raises(Exception, match="selct|syntax"):
        apply_up(conn, discover(tmp_path))
    assert applied_versions(conn) == set()
    assert "t" not in table_names(conn)


@REQUIRES_DB
def test_a_derivation_carries_the_pinned_inputs_from_issue_4(migrated):
    # The ticket's second completion criterion.
    derivation_id = insert_derivation(migrated)
    with migrated.cursor() as cur:
        cur.execute(
            "select osm_snapshot_url, osm_snapshot_seq, boundary_relation_id, "
            "boundary_buffer_m, boundary_assignment, dem_crs, dem_vertical_crs, "
            "dem_nodata_value from derivation where id = %s",
            (derivation_id,),
        )
        assert cur.fetchone() == (
            PINNED_DERIVATION["osm_snapshot_url"],
            4897,
            442461,
            10000,
            "summit",
            5514,
            8357,
            -9999.0,
        )


@REQUIRES_DB
@pytest.mark.parametrize("bad_commit", ["abc123", "z" * 40, "A" * 40, ""])
def test_engine_commit_must_be_a_full_hex_sha(migrated, bad_commit):
    import psycopg

    with pytest.raises(psycopg.errors.CheckViolation):
        insert_derivation(migrated, engine_commit=bad_commit)


@REQUIRES_DB
def test_scoring_model_rejects_an_unknown_model(migrated):
    import psycopg

    with pytest.raises(psycopg.errors.CheckViolation):
        insert_derivation(migrated, scoring_model="strava")


@REQUIRES_DB
def test_the_anchor_is_unique_within_a_derivation(migrated):
    import psycopg

    derivation_id = insert_derivation(migrated)
    insert_climb(migrated, derivation_id)
    with pytest.raises(psycopg.errors.UniqueViolation):
        insert_climb(migrated, derivation_id)


@REQUIRES_DB
def test_the_same_anchor_under_a_second_derivation_inserts_cleanly(migrated):
    # "Deriving the same kraj twice creates a second derivation and a second
    # set of climbs, never a conflict", stated as a test.
    first = insert_derivation(migrated)
    second = insert_derivation(migrated)
    insert_climb(migrated, first)
    insert_climb(migrated, second)
    with migrated.cursor() as cur:
        cur.execute("select count(*) from climb")
        assert cur.fetchone()[0] == 2


@REQUIRES_DB
def test_two_unnamed_climbs_coexist_in_one_derivation(migrated):
    # climb_slug_unique is not a NOT NULL by another name: naming is #12's
    # problem, and until then every row's slug is null.
    derivation_id = insert_derivation(migrated)
    insert_climb(migrated, derivation_id, way_refs=[1])
    insert_climb(migrated, derivation_id, way_refs=[2])
    with migrated.cursor() as cur:
        cur.execute("select count(*) from climb where slug is null")
        assert cur.fetchone()[0] == 2


@REQUIRES_DB
def test_a_climb_needs_at_least_one_way(migrated):
    import psycopg

    derivation_id = insert_derivation(migrated)
    with pytest.raises(psycopg.errors.CheckViolation):
        insert_climb(migrated, derivation_id, way_refs=[])


@REQUIRES_DB
@pytest.mark.parametrize("elevations", [[200.0], [200.0, 280.0, 300.0], []])
def test_a_profile_needs_one_elevation_per_vertex(migrated, elevations):
    # The empty list is the case a bare `array_length(...) = st_npoints(...)`
    # lets through: array_length of an empty array is null, and a null CHECK
    # passes.
    import psycopg

    climb_id = insert_climb(migrated, insert_derivation(migrated))
    with pytest.raises(psycopg.errors.CheckViolation), migrated.cursor() as cur:
        cur.execute(
            "insert into climb_profile (climb_id, geom, elevations) values (%s, %s, %s)",
            (climb_id, "SRID=4326;LINESTRING(18.29 49.83, 18.30 49.84)", elevations),
        )


@REQUIRES_DB
def test_a_profile_with_one_elevation_per_vertex_inserts(migrated):
    climb_id = insert_climb(migrated, insert_derivation(migrated))
    with migrated.cursor() as cur:
        cur.execute(
            "insert into climb_profile (climb_id, geom, elevations) values (%s, %s, %s)",
            (climb_id, "SRID=4326;LINESTRING(18.29 49.83, 18.30 49.84)", [200.0, 280.0]),
        )
        # geography, so ST_Length is metres without a reprojection.
        cur.execute("select st_length(geom) from climb_profile where climb_id = %s", (climb_id,))
        assert 1000 < cur.fetchone()[0] < 1500


@REQUIRES_DB
def test_the_resolution_apis_distance_query_finds_the_climb(migrated):
    # ST_DWithin on geography takes metres directly — the argument for
    # geography over a projected geometry, run once against the GiST index
    # meant to serve it.
    derivation_id = insert_derivation(migrated)
    climb_id = insert_climb(migrated, derivation_id)
    with migrated.cursor() as cur:
        cur.execute(
            "select id from climb where st_dwithin(top_pt, %s::geography, 150)",
            ("SRID=4326;POINT(18.3005 49.8401)",),
        )
        assert [row[0] for row in cur.fetchall()] == [climb_id]

        cur.execute(
            "select id from climb where st_dwithin(top_pt, %s::geography, 150)",
            ("SRID=4326;POINT(18.40 49.90)",),
        )
        assert cur.fetchall() == []


@REQUIRES_DB
def test_deleting_a_derivation_takes_its_climbs_and_profiles(migrated):
    # on delete cascade both ways down: a retune that is reverted leaves
    # nothing behind to be joined against.
    derivation_id = insert_derivation(migrated)
    climb_id = insert_climb(migrated, derivation_id)
    with migrated.cursor() as cur:
        cur.execute(
            "insert into climb_profile (climb_id, geom, elevations) values (%s, %s, %s)",
            (climb_id, "SRID=4326;LINESTRING(18.29 49.83, 18.30 49.84)", [200.0, 280.0]),
        )
        cur.execute("delete from derivation where id = %s", (derivation_id,))
        cur.execute("select count(*) from climb")
        assert cur.fetchone()[0] == 0
        cur.execute("select count(*) from climb_profile")
        assert cur.fetchone()[0] == 0
