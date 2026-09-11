"""Write `junctions.osm.pbf`, the extraction stage's committed fixture.

    uv run --directory derive python tests/fixtures/make_junctions_pbf.py

Both this script and its output are committed, so the tests read bytes that
were reviewed rather than bytes generated at test time. Synthetic rather than
a clip of the real extract: every way in it is here for a stated reason, the
whole file is a few kilobytes, and nothing in it depends on what OSM looked
like on the day it was made.

The geometry is a square in Moravskoslezský kraj — real enough that the
EPSG:5514 buffer the extractor takes is inside the projection's domain, small
enough to read. The square is the boundary relation; way I sits ~65 km east
of it so that the default 10 km buffer excludes it.
"""

from __future__ import annotations

from pathlib import Path

import osmium
import osmium.osm.mutable as mutable

OUTPUT = Path(__file__).with_name("junctions.osm.pbf")

# The boundary relation and its ring. Its id is passed to the extractor as
# --boundary-relation, and its version is what a derivation records: the
# extractor must read 42 out of the file rather than anything from the live
# API.
BOUNDARY_RELATION_ID = 300
BOUNDARY_RELATION_VERSION = 42
BOUNDARY_WAY_ID = 250
BOUNDARY_RING = [(90, 18.00, 49.80), (91, 18.10, 49.80), (92, 18.10, 49.90), (93, 18.00, 49.90)]

# Header values the manifest copies out of the file. The fourth one the
# manifest records, `writingprogram`, is not settable: libosmium writes its
# own string into that field and exposes it under the name `generator`, which
# is the key the extractor reads.
HEADER = {
    "osmosis_replication_timestamp": "2026-09-01T20:20:50Z",
    "osmosis_replication_sequence_number": "4897",
    "osmosis_replication_base_url": "https://example.invalid/updates",
}

# lon/lat for every node the ways below refer to. Laid out one way per
# latitude band so a failure names a way rather than a coordinate.
NODES: dict[int, tuple[float, float]] = {
    1: (18.01, 49.810),
    2: (18.02, 49.810),
    3: (18.03, 49.810),
    4: (18.04, 49.810),
    5: (18.03, 49.800),
    6: (18.03, 49.820),
    7: (18.01, 49.830),
    8: (18.02, 49.830),
    9: (18.01, 49.840),
    10: (18.02, 49.840),
    11: (18.01, 49.850),
    12: (18.02, 49.850),
    13: (18.01, 49.860),
    14: (18.02, 49.860),
    15: (18.01, 49.870),
    16: (18.02, 49.870),
    17: (18.05, 49.880),
    18: (18.06, 49.880),
    19: (18.07, 49.885),
    20: (18.07, 49.875),
    21: (18.02, 49.815),
    22: (18.02, 49.805),
    # A column of their own at 18.08–18.09 E, where no other node is, so the
    # structure ways split nothing and nothing splits them.
    40: (18.08, 49.810),
    41: (18.09, 49.810),
    42: (18.08, 49.820),
    43: (18.09, 49.820),
    44: (18.08, 49.830),
    45: (18.09, 49.830),
    46: (18.08, 49.840),
    47: (18.09, 49.840),
    30: (19.00, 49.810),
    31: (19.01, 49.810),
}

# (way id, letter, node ids, tags) — the letters are the ones the plan and
# the tests use to talk about the cases.
WAYS: list[tuple[int, str, list[int], dict[str, str]]] = [
    # A splits at 3, where B crosses it. B splits there too: one junction,
    # four segments.
    (201, "A", [1, 2, 3, 4], {"highway": "residential"}),
    (202, "B", [5, 3, 6], {"highway": "unclassified"}),
    # Out on the highway value alone.
    (203, "C", [7, 8], {"highway": "path"}),
    # A track without a surfaced tracktype is the modal Beskydy forest
    # track, and it is out; grade1 is in.
    (204, "D", [9, 10], {"highway": "track", "tracktype": "grade4"}),
    (205, "E", [11, 12], {"highway": "track", "tracktype": "grade1"}),
    # An access exclusion, and the bicycle grant that overrides it.
    (206, "F", [13, 14], {"highway": "residential", "access": "private"}),
    (207, "G", [15, 16], {"highway": "residential", "access": "private", "bicycle": "designated"}),
    # H is a lollipop: it leaves 17, reaches 18, loops through 19 and 20 and
    # returns to 18. Node 18 occurs twice in one way, which is a decision
    # point even though no second way touches it, so H splits at 18.
    #
    # #6's plan wrote this case as `17-18-17`, which does not exercise it:
    # there the repeated node is the way's own two ends, both of which cut
    # anyway, and the only interior node has degree 1. A lollipop is the
    # smallest shape where a self-visit is genuinely interior, and it is a
    # shape real OSM has.
    (208, "H", [17, 18, 19, 20, 18], {"highway": "residential"}),
    # Cyclable, well formed, and 65 km outside the buffered boundary.
    (209, "I", [30, 31], {"highway": "residential"}),
    # J crosses A at node 2 but is not cyclable, so it must not split A
    # there. Junction degree is counted over cyclable ways only.
    (210, "J", [21, 2, 22], {"highway": "path"}),
    # Off the ground, one of each: DMR 5G is bare earth, so these are what #8
    # must not read the terrain under. N says `bridge=no`, which is ground.
    (211, "K", [40, 41], {"highway": "residential", "bridge": "viaduct"}),
    (212, "L", [42, 43], {"highway": "residential", "tunnel": "yes"}),
    (213, "M", [44, 45], {"highway": "residential", "covered": "yes"}),
    (214, "N", [46, 47], {"highway": "residential", "bridge": "no"}),
]


def write(path: Path = OUTPUT) -> Path:
    header = osmium.io.Header()
    for key, value in HEADER.items():
        header.set(key, value)

    # Nodes, then ways, then the relation, each block sorted by id: the
    # order every OSM tool expects, and the order that keeps the file's
    # bytes a function of this script alone.
    writer = osmium.SimpleWriter(str(path), overwrite=True, header=header)
    try:
        locations = dict(NODES) | {node: (lon, lat) for node, lon, lat in BOUNDARY_RING}
        for node_id in sorted(locations):
            writer.add_node(mutable.Node(id=node_id, location=locations[node_id], version=1))
        for way_id, _letter, node_ids, tags in sorted(WAYS):
            writer.add_way(mutable.Way(id=way_id, nodes=node_ids, tags=tags, version=1))
        ring = [node for node, _lon, _lat in BOUNDARY_RING]
        writer.add_way(mutable.Way(id=BOUNDARY_WAY_ID, nodes=[*ring, ring[0]], version=1))
        writer.add_relation(
            mutable.Relation(
                id=BOUNDARY_RELATION_ID,
                version=BOUNDARY_RELATION_VERSION,
                members=[("w", BOUNDARY_WAY_ID, "outer")],
                tags={
                    "type": "boundary",
                    "boundary": "administrative",
                    "admin_level": "4",
                    "name": "Fixture kraj",
                },
            )
        )
    finally:
        writer.close()
    return path


if __name__ == "__main__":
    print(write())
