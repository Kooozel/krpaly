"""Which OSM ways this database considers ridden.

Its own module because it is the one decision in the extraction stage that
moves the headline climb count. It is referenced by name from
`derive/INPUTS.md` § Cyclable ways, it is versioned so a derivation records
which predicate produced it, and it is table-testable without a `.pbf`.
"""

from __future__ import annotations

from collections.abc import Mapping

# Bump on any change to the sets below, and say so in INPUTS.md: a widened
# predicate is a different database, not a bigger one, and the manifest
# records this string so two derivations that differ only here are told
# apart. Rows derived under v1 stay v1.
PREDICATE_VERSION = "cyclable/v1"

# The `highway` values a road climb can be on. `motorway` is absent and
# `motorway_link` is present on purpose: the motorway itself is not ridden,
# but a link is often the only cyclable connection between two roads that
# are.
RIDDEN_HIGHWAY = frozenset(
    {
        "motorway_link",
        "trunk",
        "trunk_link",
        "primary",
        "primary_link",
        "secondary",
        "secondary_link",
        "tertiary",
        "tertiary_link",
        "unclassified",
        "residential",
        "living_street",
        "road",
        "cycleway",
    }
)

# krpaly is a road-climb database and the milestone's headline number is the
# climb count, so an untagged forest track — the modal `track` in the
# Beskydy — must not inflate it. grade1 and grade2 are the surfaced ones;
# grade3 to grade5 and an absent `tracktype` are out. Widening this is a
# `cyclable/v2` and a new derivation, which is exactly the visibility
# wanted.
RIDDEN_TRACKTYPE = frozenset({"grade1", "grade2"})

# Legal exclusions. These are about permission rather than surface, so a
# `bicycle` tag that grants permission overrides them.
BARRED_ACCESS = frozenset({"private", "no"})

# ...and the values that grant it. `bicycle=no` is the one direction this
# cannot go: it is an exclusion no `access` value undoes.
GRANTED_BICYCLE = frozenset({"yes", "designated", "permissive"})


def is_cyclable(tags: Mapping[str, str]) -> bool:
    """Ways this database considers ridden. See derive/INPUTS.md § Cyclable ways.

    A `bicycle` grant reopens a way an `access` value closed, but it never
    promotes a `highway` value that is out of the list — a `path` signed for
    bicycles is still not a road climb, and letting it in is what a
    `cyclable/v2` would be for.
    """
    highway = tags.get("highway")
    if highway is None:
        return False

    if highway == "track":
        if tags.get("tracktype") not in RIDDEN_TRACKTYPE:
            return False
    elif highway not in RIDDEN_HIGHWAY:
        return False

    if tags.get("bicycle") == "no":
        return False

    if tags.get("access") in BARRED_ACCESS:
        return tags.get("bicycle") in GRANTED_BICYCLE

    return True
