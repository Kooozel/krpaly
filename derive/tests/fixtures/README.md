# Test fixtures

## `junctions.osm.pbf`

**Synthetic. No OpenStreetMap data is redistributed here, so no ODbL obligation attaches to this
file.** Every node, way and relation in it was written by `make_junctions_pbf.py`, which is
committed beside it; nothing was cut from a Geofabrik extract or copied from the OSM database.

`CONTRIBUTING.md` § "Test data" and `ATTRIBUTION.md` § "Committed fixtures" anticipated a *cut*
fixture — one okres or less — and require it to carry the *© OpenStreetMap contributors* credit
together with the extract, snapshot timestamp and bounding box it was cut from. This file is the
other kind, and this README is where that is recorded: the rule's obligation is discharged by
there being no OSM data in the file, not skipped.

**Why synthetic.** The fixture exists to pin the junction split, the tag predicate and the
boundary buffer, and each of those needs a case that a real clip only supplies by luck — a way
that revisits its own node, an `access=private` way reopened by `bicycle=designated`, a
non-cyclable way crossing a cyclable one at an interior node. Written by hand, every case in the
file is deliberate and the whole file is under a kilobyte. Cut from a real extract, it would be
several megabytes of mostly-irrelevant geometry that a later OSM edit could quietly change the
meaning of.

### What it contains

A square boundary relation — id `300`, version `42`, `type=boundary`, `admin_level=4`, spanning
18,00–18,10 °E and 49,80–49,90 °N, which is inside Moravskoslezský kraj and therefore inside the
domain of EPSG:5514, the projection the extractor buffers in — and ten ways lettered A to J.
`make_junctions_pbf.py` names each one and what it is there to prove;
`../test_extract.py` asserts the segments they must produce.

### Regenerating it

```sh
uv run --directory derive python tests/fixtures/make_junctions_pbf.py
```

The bytes are a function of the script and of the pinned `osmium` in `derive/uv.lock`, so a
regeneration under the same lockfile reproduces the committed file exactly. Commit the result
alongside any change to the script — the tests read the committed bytes, which is the point: they
are bytes that were reviewed rather than bytes generated at test time.
