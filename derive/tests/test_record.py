"""The committed copy of a stage manifest: the manifest minus `run`."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from krpaly_derive import record
from krpaly_derive.record import RecordError, record_dir, write_record

NAME = "dem.manifest.json"

# A URL, a relative tile path and WKT: none of it is a machine path, so all of
# it has to pass.
MANIFEST = {
    "source": {"file": "candidates.parquet", "sha256": "ab" * 32},
    "boundary": {"wkt": "POLYGON ((0 0, 1 0, 1 1, 0 0))"},
    "dem": {"route": "https://ags.cuzk.cz/arcgis2/rest/services/dmr5g/ImageServer"},
    "tiles": [{"file": "dem/x-1_y2.tif", "bbox": [-1.0, 2.0, 3.0, 4.0]}],
    "output": {"vrt": "dem.vrt", "dir": "dem"},
    "run": {"started_at": "2026-09-11T10:00:00+00:00", "wall_clock_s": 1.0},
}


def test_two_runs_over_one_input_write_the_same_bytes(tmp_path: Path) -> None:
    """The issue's churn case: a verification re-run must not show up in git."""
    first = write_record(MANIFEST, tmp_path / "first", NAME)
    rerun = {
        **MANIFEST,
        "run": {"started_at": "2026-09-12T08:00:00+00:00", "candidates_path": "/home/x.parquet"},
    }
    second = write_record(rerun, tmp_path / "second", NAME)

    assert first == tmp_path / "first" / NAME
    assert first.read_bytes() == second.read_bytes()
    text = first.read_text()
    assert json.loads(text) == {block: v for block, v in MANIFEST.items() if block != "run"}
    assert text == json.dumps(json.loads(text), indent=2, sort_keys=True) + "\n"


def test_a_machine_path_outside_run_is_refused_by_key(tmp_path: Path) -> None:
    leaky = {
        **MANIFEST,
        "osm_snapshot": {"path": "/home/kozel/data/czech-republic.osm.pbf"},
        "tiles": [{"file": "/srv/dem/x-1_y2.tif"}],
    }
    with pytest.raises(RecordError) as raised:
        write_record(leaky, tmp_path / "record", NAME)

    message = str(raised.value)
    assert "osm_snapshot.path" in message
    assert "tiles.0.file" in message
    assert "--force" in message
    assert not (tmp_path / "record").exists()


def test_a_machine_path_inside_run_is_dropped_not_refused(tmp_path: Path) -> None:
    manifest = {**MANIFEST, "run": {"pbf_path": "/home/kozel/data/czech-republic.osm.pbf"}}
    written = write_record(manifest, tmp_path / "record", NAME)
    assert "/home" not in written.read_text()


def test_the_record_directory_is_named_after_out(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(record, "RECORD_ROOT", tmp_path / "manifests")
    assert record_dir(Path("data/kraj-1")) == tmp_path / "manifests" / "kraj-1"
    assert record_dir(tmp_path / "elsewhere" / "kraj-1") == tmp_path / "manifests" / "kraj-1"
