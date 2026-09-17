"""The load stage, over inputs written in the earlier stages' own schemas.

Split as test_migrate.py is. Rebuilding a profile, assigning a kraj and vouching
for the inputs are pure over Parquet and the committed fixture `.pbf`, and run
anywhere — no Node and no network. Loading needs a real PostGIS, because what
is worth asserting there is the database's behaviour: a transaction rolled
back, a check constraint met, a geography that round-trips. Those carry a
skipif on DATABASE_URL, which CI always sets.
"""

from __future__ import annotations

import json
import os
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

import numpy as np
import psycopg
import pyarrow as pa
import pytest
import shapely
from psycopg.rows import dict_row

from krpaly_derive import load as load_module
from krpaly_derive.anchor import MANIFEST_NAME as ANCHORS_MANIFEST
from krpaly_derive.anchor import OUTPUT_NAME as ANCHORS_NAME
from krpaly_derive.anchor import SCHEMA as ANCHORS_SCHEMA
from krpaly_derive.dem import MANIFEST_NAME as DEM_MANIFEST
from krpaly_derive.detect import MANIFEST_NAME as CLIMBS_MANIFEST
from krpaly_derive.detect import derivation_block, read_version
from krpaly_derive.extract import (
    DEFAULT_INDEX,
    FORWARD,
    candidate,
    sha256_of,
    write_parquet,
    write_table,
)
from krpaly_derive.extract import MANIFEST_NAME as CANDIDATES_MANIFEST
from krpaly_derive.extract import OUTPUT_NAME as CANDIDATES_NAME
from krpaly_derive.load import MANIFEST_NAME as LOAD_MANIFEST
from krpaly_derive.load import (
    LoadError,
    assign_region,
    main,
    profile_of,
    read_regions,
    region_of,
)
from krpaly_derive.migrate import apply_up, discover
from krpaly_derive.runs import Profile
from krpaly_derive.sample import OUTPUT_NAME as PROFILES_NAME
from krpaly_derive.sample import SCHEMA as PROFILES_SCHEMA

REQUIRES_DB = pytest.mark.skipif(
    os.environ.get("DATABASE_URL") is None,
    reason="needs a PostGIS database; set DATABASE_URL",
)

FIXTURE = Path(__file__).parent / "fixtures" / "junctions.osm.pbf"

# The fixture's square boundary relation, spanning 18,00–18,10 °E and
# 49,80–49,90 °N. Loaded here under a real kraj's code, as the only one it has.
FIXTURE_RELATION = 300

SNAPSHOT_URL = "https://download.geofabrik.de/europe/czech-republic-260901.osm.pbf"

# Three candidates: a chain of two up a meridian inside the square, and one
# 18 km east of it, which a 10 km buffer would still have extracted.
SEGMENTS = [
    (101, [1, 2], [(18.05, 49.82), (18.05, 49.83)]),
    (102, [2, 3], [(18.05, 49.83), (18.05, 49.84)]),
    (103, [7, 8], [(18.30, 49.85), (18.30, 49.86)]),
]


def a_climb(climb_id: int, candidate_ids: list[int], way_refs: list[int], **overrides) -> dict:
    """One row of #10's anchors, in the schema it writes."""
    return {
        "climb_id": climb_id,
        "way_refs": way_refs,
        "start_node_id": SEGMENTS[candidate_ids[0]][1][0],
        "end_node_id": SEGMENTS[candidate_ids[-1]][1][-1],
        "candidate_ids": candidate_ids,
        "start_offset_m": 0.0,
        "end_offset_m": 30.0,
        "dist_m": 30.0,
        "gain_m": 3.0,
        "avg_grade_pct": 10.0,
        "max_grade_pct": 12.5,
        "start_lat": 49.82,
        "start_lon": 18.05,
        "top_lat": 49.84,
        "top_lon": 18.05,
        "difficulty": None,
        "category": None,
        "run_id": climb_id,
        "climb_index": 0,
        "collapsed": 0,
    } | overrides


THREE_CLIMBS = [
    # Over both candidates, opening 10 m into the first.
    a_climb(
        0,
        [0, 1],
        [101, 102],
        start_offset_m=10.0,
        end_offset_m=60.0,
        dist_m=50.0,
        gain_m=5.0,
        difficulty=112.5,
        category="4",
    ),
    a_climb(1, [1], [102]),
    # Tops out east of the square, so outside every kraj the fixture has.
    a_climb(2, [2], [103], start_lon=18.30, top_lat=49.86, top_lon=18.30),
]


def write_inputs(out: Path, record: Path, climbs: list[dict] = THREE_CLIMBS) -> None:
    """#6's candidates, #8's profiles, #10's anchors and the manifests vouching for them.

    Each profile is four samples 10 m apart, rising 1 m per sample.
    """
    out.mkdir(parents=True, exist_ok=True)
    record.mkdir(parents=True, exist_ok=True)
    candidates = out / CANDIDATES_NAME
    write_parquet(
        [candidate(way, nodes, coords, FORWARD, None) for way, nodes, coords in SEGMENTS],
        candidates,
    )

    def latitudes(coords: list[tuple[float, float]]) -> list[float]:
        (_, south), (_, north) = coords
        return np.linspace(south, north, 4).tolist()

    profiles = out / PROFILES_NAME
    write_table(
        pa.table(
            {
                "candidate_id": pa.array(range(len(SEGMENTS)), pa.uint64()),
                "n_samples": pa.array([4] * len(SEGMENTS), pa.int32()),
                "distance_m": [[0.0, 10.0, 20.0, 30.0] for _ in SEGMENTS],
                "elevation_m": [[300.0, 301.0, 302.0, 303.0] for _ in SEGMENTS],
                "lat": [latitudes(coords) for _, _, coords in SEGMENTS],
                "lon": [[coords[0][0]] * 4 for _, _, coords in SEGMENTS],
            },
            schema=PROFILES_SCHEMA,
        ),
        profiles,
    )
    anchors = out / ANCHORS_NAME
    write_table(pa.Table.from_pylist(climbs, schema=ANCHORS_SCHEMA), anchors)

    # Only the blocks this stage reads, with the values the real run recorded.
    manifests = {
        out / CANDIDATES_MANIFEST: {
            "osm_snapshot": {
                "file": FIXTURE.name,
                "sha256": sha256_of(FIXTURE),
                "replication_seq": "4897",
                "replication_ts": "2026-09-01T20:20:50Z",
            },
            "boundary": {
                "relation_id": FIXTURE_RELATION,
                "relation_version": 42,
                "buffer_m": 10000.0,
                "assignment": "summit",
            },
            "way_filter": {"version": "cyclable/v2", "structure": "structure/v1"},
            "output": {"sha256": sha256_of(candidates)},
        },
        out / CLIMBS_MANIFEST: {
            "derivation": derivation_block(read_version(), {}, "aso"),
            "output": {"sha256": "c" * 64},
        },
        out / ANCHORS_MANIFEST: {
            "source": {
                "climbs_sha256": "c" * 64,
                "profiles_sha256": sha256_of(profiles),
                "candidates_sha256": sha256_of(candidates),
            },
            "output": {"sha256": sha256_of(anchors)},
        },
        record / DEM_MANIFEST: {
            "dem": {
                "product": "ZABAGED® – Výškopis – DMR 5G (64111), via the ImageServer 2 m mosaic",
                "route": "https://ags.cuzk.gov.cz/arcgis2/rest/services/dmr5g/ImageServer/exportImage",
                "resolution_m": 2,
                "crs": 5514,
                "vertical_crs": 8357,
                "nodata_value": -9999.0,
                "fetched_at": "2026-09-11T15:36:02+00:00",
            },
            "source": {"sha256": sha256_of(candidates)},
        },
    }
    for path, manifest in manifests.items():
        path.write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n")


def edit_manifest(path: Path, block: str, key: str, value: object) -> None:
    manifest = json.loads(path.read_text())
    manifest[block][key] = value
    path.write_text(json.dumps(manifest))


def load(out: Path, *extra: str) -> int:
    return main(
        [
            "--out",
            str(out),
            "--record",
            str(out.parent / "record"),
            "--pbf",
            str(FIXTURE),
            "--osm-snapshot-url",
            SNAPSHOT_URL,
            *extra,
        ]
    )


def a_profile(start_node: int, end_node: int, distances: list[float], lon: float) -> Profile:
    """A candidate's samples along one meridian, elevation rising 1 m per 10 m."""
    distance = np.array(distances, dtype=np.float64)
    return Profile(
        start_node_id=start_node,
        end_node_id=end_node,
        distance_m=distance,
        elevation_m=(300.0 + distance / 10.0).astype(np.float32),
        lat=np.linspace(49.85, 49.86, len(distances)),
        lon=np.full(len(distances), lon),
    )


# --- rebuilding the profile ------------------------------------------------


def test_a_window_inside_the_second_candidate_keeps_exactly_the_samples_in_it() -> None:
    # Candidate 1's distances start at 5 m: join_profiles re-bases each one, so
    # the window is on the joined ruler, where candidate 1 opens at 30 m.
    profiles = {
        0: a_profile(1, 2, [0.0, 10.0, 20.0, 30.0], 18.05),
        1: a_profile(2, 3, [5.0, 15.0, 25.0, 35.0, 45.0], 18.06),
    }
    samples = profile_of([0, 1], profiles, 40.0, 60.0)

    assert [d for d, _, _, _ in samples] == [40.0, 50.0, 60.0]
    assert all(lon == 18.06 for _, _, _, lon in samples)


@pytest.mark.parametrize(("start", "end"), [(45.0, 55.0), (41.0, 49.0)])
def test_a_window_holding_fewer_than_two_samples_has_no_profile(start: float, end: float) -> None:
    # One sample or none is not a linestring. None rather than a short list, so
    # the loader has to decide what to do with it instead of writing it.
    profiles = {0: a_profile(1, 2, [0.0, 10.0, 20.0, 30.0, 40.0, 50.0, 60.0], 18.05)}
    assert profile_of([0], profiles, start, end) is None


# --- assigning a kraj --------------------------------------------------------


def test_a_summit_is_assigned_to_the_kraj_it_is_in_and_to_none_outside_czechia() -> None:
    regions = [
        region_of("CZ072", shapely.box(17.9, 49.0, 18.0, 49.1)),
        region_of("CZ080", shapely.box(18.0, 49.0, 18.1, 49.1)),
    ]
    assert assign_region(regions, lat=49.05, lon=17.95) == "CZ072"
    assert assign_region(regions, lat=49.05, lon=18.05) == "CZ080"
    # A 10 km buffer around Moravskoslezský reaches into Poland.
    assert assign_region(regions, lat=49.95, lon=18.05) is None


def test_a_summit_exactly_on_a_shared_border_goes_to_the_lower_code() -> None:
    # Given in the other order, so it is the code deciding and not the list.
    regions = [
        region_of("CZ080", shapely.box(18.0, 49.0, 18.1, 49.1)),
        region_of("CZ072", shapely.box(17.9, 49.0, 18.0, 49.1)),
    ]
    assert assign_region(regions, lat=49.05, lon=18.0) == "CZ072"


def test_the_fixtures_boundary_relation_assembles_into_the_kraj_it_is_coded_as() -> None:
    (region,) = read_regions(FIXTURE, {FIXTURE_RELATION: "CZ080"}, DEFAULT_INDEX)
    assert region.code == "CZ080"
    assert assign_region([region], lat=49.85, lon=18.05) == "CZ080"
    # Unbuffered: 2 km east of the square is outside it, where extraction's
    # 10 km buffer would still have taken it in.
    assert assign_region([region], lat=49.85, lon=18.13) is None


def test_a_kraj_whose_relation_is_not_in_the_extract_is_fatal_and_named() -> None:
    # A summit assigned against thirteen kraje instead of fourteen is dropped
    # as outside Czechia, with nothing to say why.
    with pytest.raises(LoadError, match="442449"):
        read_regions(FIXTURE, {FIXTURE_RELATION: "CZ080", 442449: "CZ072"}, DEFAULT_INDEX)


# --- vouching for the inputs -------------------------------------------------


@pytest.fixture
def inputs(tmp_path: Path) -> Path:
    out = tmp_path / "kraj-1"
    write_inputs(out, tmp_path / "record")
    return out


def test_anchors_their_manifest_does_not_vouch_for_are_refused(inputs: Path) -> None:
    write_table(
        pa.Table.from_pylist(THREE_CLIMBS[:1], schema=ANCHORS_SCHEMA), inputs / ANCHORS_NAME
    )
    with pytest.raises(SystemExit, match="re-run krpaly_derive.anchor"):
        load(inputs)


def test_anchors_over_other_candidates_are_refused(inputs: Path) -> None:
    edit_manifest(inputs / ANCHORS_MANIFEST, "source", "candidates_sha256", "0" * 64)
    with pytest.raises(SystemExit, match="other candidates.*re-run krpaly_derive.anchor"):
        load(inputs)


def test_a_climbs_manifest_for_other_climbs_is_refused(inputs: Path) -> None:
    # It is where the engine columns of the row come from.
    edit_manifest(inputs / CLIMBS_MANIFEST, "output", "sha256", "d" * 64)
    with pytest.raises(SystemExit, match="other climbs"):
        load(inputs)


def test_a_pbf_that_is_not_the_extracted_snapshot_is_refused(inputs: Path) -> None:
    # The kraj polygons are read from --pbf: a boundary from another snapshot
    # is a wrong region_code with nothing in the row to say so.
    edit_manifest(inputs / CANDIDATES_MANIFEST, "osm_snapshot", "sha256", "0" * 64)
    with pytest.raises(SystemExit, match="not the snapshot"):
        load(inputs)


def test_a_missing_dem_record_is_refused(inputs: Path) -> None:
    (inputs.parent / "record" / DEM_MANIFEST).unlink()
    with pytest.raises(SystemExit, match="run krpaly_derive.dem"):
        load(inputs)


def test_a_dem_record_fetched_for_other_candidates_is_refused(inputs: Path) -> None:
    edit_manifest(inputs.parent / "record" / DEM_MANIFEST, "source", "sha256", "0" * 64)
    with pytest.raises(SystemExit, match="other candidates.*re-run krpaly_derive.dem"):
        load(inputs)


# --- the database ------------------------------------------------------------


@pytest.fixture
def conn():
    with psycopg.connect(os.environ["DATABASE_URL"], row_factory=dict_row) as connection:
        with connection.cursor() as cur:
            cur.execute(
                "drop table if exists climb_profile, climb, derivation, region, "
                "schema_migrations cascade"
            )
        connection.commit()
        yield connection


@pytest.fixture
def migrated(conn, inputs: Path):
    """The schema, with the fixture's square standing in for the only kraj.

    The other thirteen are deleted rather than left in: their relations are not
    in the fixture, and a kraj missing from the extract is fatal by design.
    """
    apply_up(conn, discover())
    with conn.cursor() as cur:
        cur.execute("delete from region where code <> 'CZ080'")
        cur.execute("update region set osm_relation_id = %s", (FIXTURE_RELATION,))
    conn.commit()
    return conn


def one(conn, query: str, *params: object) -> dict:
    with conn.cursor() as cur:
        cur.execute(query, params)
        return cur.fetchone()


def all_of(conn, query: str, *params: object) -> list[dict]:
    with conn.cursor() as cur:
        cur.execute(query, params)
        return cur.fetchall()


@REQUIRES_DB
def test_a_load_fills_every_derivation_column_from_the_manifests(migrated, inputs: Path) -> None:
    assert load(inputs) == 0

    row = one(migrated, "select * from derivation")
    del row["id"], row["created_at"]
    assert row == {
        "engine_version": "v0.1.0",
        "engine_commit": "9fb96def4e9f9d9a3487c1c4701246ec1c42579d",
        # An object, not jsonb 'null' — the constraint 0005 wrote.
        "engine_config_override": {},
        "scoring_model": "aso",
        "osm_snapshot_url": SNAPSHOT_URL,
        "osm_snapshot_sha256": sha256_of(FIXTURE),
        "osm_snapshot_replication_ts": datetime(2026, 9, 1, 20, 20, 50, tzinfo=UTC),
        "osm_snapshot_seq": 4897,
        "boundary_relation_id": FIXTURE_RELATION,
        "boundary_relation_version": 42,
        "boundary_buffer_m": 10000,
        "boundary_assignment": "summit",
        "dem_product": "ZABAGED® – Výškopis – DMR 5G (64111), via the ImageServer 2 m mosaic",
        "dem_route": "https://ags.cuzk.gov.cz/arcgis2/rest/services/dmr5g/ImageServer/exportImage",
        "dem_resolution_m": Decimal(2),
        "dem_crs": 5514,
        "dem_vertical_crs": 8357,
        "dem_nodata_value": -9999.0,
        "dem_manifest_sha256": sha256_of(inputs.parent / "record" / DEM_MANIFEST),
        "dem_fetched_at": datetime(2026, 9, 11, 15, 36, 2, tzinfo=UTC),
        "way_filter_version": "cyclable/v2",
        "structure_version": "structure/v1",
    }


@REQUIRES_DB
def test_each_climb_lands_with_its_anchor_its_grades_unconverted_and_its_kraj(
    migrated, inputs: Path
) -> None:
    assert load(inputs) == 0

    rows = all_of(
        migrated,
        "select way_refs, start_node_id, end_node_id, region_code, dist_m, gain_m, avg_grade, "
        "max_grade, difficulty, category, slug, name, "
        "st_x(top_pt::geometry) as top_lon, st_y(top_pt::geometry) as top_lat "
        "from climb order by way_refs",
    )
    assert rows == [
        {
            "way_refs": [101, 102],
            "start_node_id": 1,
            "end_node_id": 3,
            "region_code": "CZ080",
            "dist_m": 50.0,
            "gain_m": 5.0,
            "avg_grade": 10.0,
            # Per cent already: detect converted it, and a second × 100 here
            # would make this 1250.
            "max_grade": 12.5,
            "difficulty": 112.5,
            "category": "4",
            "slug": None,
            "name": None,
            "top_lon": 18.05,
            "top_lat": 49.84,
        },
        {
            "way_refs": [102],
            "start_node_id": 2,
            "end_node_id": 3,
            "region_code": "CZ080",
            "dist_m": 30.0,
            "gain_m": 3.0,
            "avg_grade": 10.0,
            "max_grade": 12.5,
            # Null is data: the scoring model cleared no threshold.
            "difficulty": None,
            "category": None,
            "slug": None,
            "name": None,
            "top_lon": 18.05,
            "top_lat": 49.84,
        },
    ]


@REQUIRES_DB
def test_a_profile_is_the_joined_samples_inside_the_detection_window(
    migrated, inputs: Path
) -> None:
    assert load(inputs) == 0

    rows = all_of(
        migrated,
        "select c.way_refs, st_npoints(p.geom::geometry) as points, p.elevations, "
        "st_y(st_startpoint(p.geom::geometry)) as start_lat "
        "from climb_profile p join climb c on c.id = p.climb_id order by c.way_refs",
    )
    # Climb 0 opens 10 m into candidate 0 and runs 60 m, over the junction
    # vertex the two candidates share, which is counted once.
    assert rows[0]["way_refs"] == [101, 102]
    assert rows[0]["elevations"] == [301.0, 302.0, 303.0, 301.0, 302.0, 303.0]
    assert rows[0]["points"] == 6
    assert rows[0]["start_lat"] == pytest.approx(49.82 + 0.01 / 3)
    assert rows[1]["way_refs"] == [102]
    assert rows[1]["elevations"] == [300.0, 301.0, 302.0, 303.0]
    assert rows[1]["points"] == 4


@REQUIRES_DB
def test_climbs_outside_czechia_or_too_short_to_draw_are_counted_not_loaded(
    migrated, inputs: Path
) -> None:
    # Offsets between two samples 10 m apart: no sample survives the clip.
    short = a_climb(3, [0], [101], start_offset_m=12.0, end_offset_m=18.0)
    write_inputs(inputs, inputs.parent / "record", [*THREE_CLIMBS, short])
    assert load(inputs) == 0

    assert one(migrated, "select count(*) from climb")["count"] == 2
    counts = json.loads((inputs / LOAD_MANIFEST).read_text())["counts"]
    assert counts == {
        "climbs": 4,
        "loaded": 2,
        "outside_czechia": 1,
        "degenerate_profiles": 1,
        "by_region": {"CZ080": 2},
        "profile_points": 10,
    }


@REQUIRES_DB
def test_a_load_that_fails_partway_leaves_no_derivation_behind(
    migrated, inputs: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The profiles are written last, so failing them fails the load after the
    # derivation row and every climb are already in. A half-loaded kraj looks
    # finished; an empty one does not.
    written = []

    def fail_after_the_climbs(cur, ids, climbs) -> None:
        cur.execute("select count(*) from climb")
        written.append(cur.fetchone()[0])
        raise RuntimeError("the profile COPY failed")

    monkeypatch.setattr(load_module, "copy_profiles", fail_after_the_climbs)
    with pytest.raises(RuntimeError, match="profile COPY"):
        load(inputs)

    assert written == [2]
    assert one(migrated, "select count(*) from derivation")["count"] == 0
    assert one(migrated, "select count(*) from climb")["count"] == 0


@REQUIRES_DB
def test_the_same_inputs_again_are_refused_unless_forced(migrated, inputs: Path) -> None:
    assert load(inputs) == 0
    with pytest.raises(SystemExit, match="already records exactly these inputs"):
        load(inputs)
    assert one(migrated, "select count(*) from derivation")["count"] == 1


@REQUIRES_DB
def test_a_forced_second_load_is_a_second_derivation_with_the_same_record(
    migrated, inputs: Path
) -> None:
    record = inputs.parent / "record" / LOAD_MANIFEST
    assert load(inputs) == 0
    first = record.read_bytes()
    assert load(inputs, "--force") == 0

    per_derivation = all_of(
        migrated, "select derivation_id, count(*) from climb group by 1 order by 1"
    )
    assert [row["count"] for row in per_derivation] == [2, 2]
    # The ids that differ live under `run`, which the record leaves out.
    assert record.read_bytes() == first
    committed = json.loads(first)
    assert "run" not in committed
    assert str(inputs.parent) not in first.decode()
    working = json.loads((inputs / LOAD_MANIFEST).read_text())
    assert working["run"]["derivation_id"] == per_derivation[1]["derivation_id"]
