"""
compute_scope on the Task 119 requirement case:

    PMA -> projectA -> riskA -> riskB <- projectB -> riskC
    PMB -> projectB -> riskC

The fake session answers the scope query by walking an in-memory graph the
way apoc.path.subgraphNodes does, driven by the relationshipFilter strings
compute_scope builds.
"""
import asyncio

import pytest

from bluestar_mcp_shared import rebac
from bluestar_mcp_shared.ontology import shape_relationship_types
from bluestar_mcp_shared.rebac import SYSTEM_LABELS, Scope, compute_scope, load_label_key_map, scope_query

NODES = {
    "u-pma": (["BS_user"], {"email": "pma@example.com", "is_active": True}),
    "u-pmb": (["BS_user"], {"email": "pmb@example.com", "is_active": True}),
    "u-old": (["BS_user"], {"email": "old@example.com", "is_active": False}),
    "pm-a": (["ProjectManager"], {"email": "pma@example.com"}),
    "pm-b": (["ProjectManager"], {"email": "pmb@example.com"}),
    "projectA": (["Project"], {"id": "P-A"}),
    "projectB": (["Project"], {"id": "P-B"}),
    "riskA": (["Risk"], {"id": "R-A"}),
    "riskB": (["Risk"], {"id": "R-B"}),
    "riskC": (["Risk"], {"id": "R-C"}),
    "state": (["State"], {"id": "open"}),
}
EDGES = [
    ("IS_SAME_PERSON", "u-pma", "pm-a"), ("IS_SAME_PERSON", "u-pmb", "pm-b"),
    ("IS_SAME_PERSON", "u-old", "pm-a"),
    ("IS_PM_OF", "pm-a", "projectA"), ("IS_PM_OF", "pm-b", "projectB"),
    ("HAS_RAID", "projectA", "riskA"), ("RELATES_TO", "riskA", "riskB"),
    ("HAS_RAID", "projectB", "riskB"), ("HAS_RAID", "projectB", "riskC"),
    ("HAS_STATE", "riskA", "state"),
]
REL_TYPES = shape_relationship_types([
    {"name": "HAS_RAID", "from_types": ["Project"], "to_types": ["Risk"], "rebac_out": "propagate", "rebac_in": "reference"},
    {"name": "RELATES_TO", "from_types": ["Risk"], "to_types": ["Risk"], "rebac_out": "reference", "rebac_in": "none"},
    {"name": "IS_PM_OF", "from_types": ["ProjectManager"], "to_types": ["Project"], "rebac_out": "propagate"},
])
KEY_MAP = {"Project": "id", "Risk": "id", "ProjectManager": "email", "Chunk": "id"}


def parse_filter(relationship_filter: str) -> set:
    allowed = set()
    for part in relationship_filter.split("|"):
        if (part.startswith("<") and part.endswith(">")) or not (part.startswith("<") or part.endswith(">")):
            allowed |= {(part.strip("<>"), "out"), (part.strip("<>"), "in")}
        elif part.endswith(">"):
            allowed.add((part[:-1], "out"))
        else:
            allowed.add((part[1:], "in"))
    return allowed


def walk(starts, relationship_filter, max_level=None, edges=EDGES):
    allowed = parse_filter(relationship_filter)
    visited, frontier, reached, level = set(starts), list(starts), [], 0
    while frontier and (max_level is None or level < max_level):
        level += 1
        nxt = []
        for node in frontier:
            for rel_type, src, dst in edges:
                if src == node and (rel_type, "out") in allowed:
                    other = dst
                elif dst == node and (rel_type, "in") in allowed:
                    other = src
                else:
                    continue
                if other not in visited:
                    visited.add(other)
                    reached.append(other)
                    nxt.append(other)
        frontier = nxt
    return reached


class Result:
    def __init__(self, rows):
        self.rows = rows

    async def data(self):
        return self.rows


class GraphSession:
    """A session whose scope query walks NODES/EDGES; db.relationshipTypes lists EDGES' types."""

    def __init__(self, kg_types=None):
        self.calls = []
        self.kg_types = kg_types

    def find(self, query, params):
        label = query.split("MATCH (s:`", 1)[1].split("`", 1)[0]
        key_prop = query.split("{`", 1)[1].split("`", 1)[0]
        key = params[rebac.start_param(key_prop)]
        for node_id, (labels, props) in NODES.items():
            if label in labels and props.get(key_prop) == key:
                if "BS_user" in labels and not props.get("is_active", True):
                    return None
                return node_id
        return None

    async def run(self, query, **params):
        self.calls.append((query, params))
        if "db.relationshipTypes" in query:
            types = self.kg_types if self.kg_types is not None else sorted({e[0] for e in EDGES})
            return Result([{"relationshipType": t} for t in types])
        start = self.find(query, params)
        if start is None:
            return Result([])
        full = walk([start], params["propagate_filter"])
        readonly = []
        if "$reference_filter" in query:
            readonly = [n for n in walk([start] + full, params["reference_filter"], params["reference_depth"])
                        if n not in full]
        return Result([{"labels": NODES[n][0], "n": NODES[n][1], "tier": "full"} for n in full]
                      + [{"labels": NODES[n][0], "n": NODES[n][1], "tier": "readonly"} for n in readonly])


def scope_from(start, depth=1, rel_types=REL_TYPES, session=None):
    session = session or GraphSession()
    return asyncio.run(compute_scope(session, start, rel_types, KEY_MAP, reference_depth=depth)), session


def test_pma_from_their_bs_user():
    scope, _ = scope_from(("BS_user", "pma@example.com"))
    assert scope.full == {"ProjectManager": {"pma@example.com"}, "Project": {"P-A"}, "Risk": {"R-A"}}
    assert scope.readonly == {"Risk": {"R-B"}}


def test_pmb_from_their_bs_user():
    scope, _ = scope_from(("BS_user", "pmb@example.com"))
    assert scope.full == {"ProjectManager": {"pmb@example.com"}, "Project": {"P-B"}, "Risk": {"R-B", "R-C"}}
    assert scope.readonly == {}


def test_from_any_node():
    scope, _ = scope_from(("Project", "P-A"))
    assert scope.full == {"Risk": {"R-A"}}
    assert scope.readonly == {"Risk": {"R-B"}}


def test_reference_depth_2():
    scope, _ = scope_from(("BS_user", "pma@example.com"), depth=2)
    assert scope.readonly == {"Risk": {"R-B"}, "Project": {"P-B"}}
    assert "R-C" not in scope.visible["Risk"]


def test_inactive_bs_user_or_missing_start_gives_empty_scope():
    assert scope_from(("BS_user", "old@example.com"))[0] == Scope()
    assert scope_from(("Project", "P-Z"))[0] == Scope()


def test_unknown_start_label_is_a_warning_and_no_query():
    scope, session = scope_from(("Nonsense", "x"))
    assert scope == Scope()
    assert session.calls == []
    assert scope.warnings == ["Start label 'Nonsense' has no known key property — nothing to walk from"]


def test_filters_and_query_shape():
    scope, session = scope_from(("BS_user", "pma@example.com"))
    assert scope.filters == (
        "_CREATED>|_UPDATED>|IS_SAME_PERSON>|IN_DOMAIN>|HAS_CHUNK>|HAS_EXTRACTED_ENTITY>|HAS_RAID>|IS_PM_OF>",
        "<HAS_RAID|RELATES_TO>",
    )
    query, params = session.calls[0]
    assert query.startswith("MATCH (s:`BS_user` {`email`: $email}) WHERE NOT s:BS_user OR coalesce(s.is_active, true)")
    assert params["email"] == "pma@example.com"
    assert query.count("apoc.path.subgraphNodes") == 2
    assert params["reference_depth"] == 1


def test_no_reference_types_skips_the_reference_walk():
    defaults = shape_relationship_types([{"name": n} for n in ("HAS_RAID", "RELATES_TO", "IS_PM_OF")])
    scope, session = scope_from(("BS_user", "pma@example.com"), rel_types=defaults)
    assert session.calls[0][0].count("apoc.path.subgraphNodes") == 1
    assert scope.full["Risk"] == {"R-A", "R-B"} and scope.readonly == {}


def test_system_labels_and_unkeyed_nodes_are_not_collected():
    scope, _ = scope_from(("BS_user", "pma@example.com"))
    assert not set(scope.full) & SYSTEM_LABELS


def test_warnings_for_invalid_modes_duplicates_and_unwalked_types():
    rel_types = list(REL_TYPES.values()) + [
        {"name": "ODD", "rebac_out": "sideways", "rebac_in": "none"},
        {"name": "HAS_RAID", "rebac_out": "propagate", "rebac_in": "none"},
    ]
    session = GraphSession(kg_types=["HAS_RAID", "IS_PM_OF", "RELATES_TO", "HAS_STATE", "_CREATED", "LEGACY_LINK"])
    scope, _ = scope_from(("BS_user", "pma@example.com"), rel_types=rel_types, session=session)
    assert scope.unwalked_types == ["LEGACY_LINK"]
    assert scope.warnings == [
        "RelationshipType 'ODD' has rebac_out = 'sideways', not one of propagate, reference, none — ReBAC treats it as none",
        "RelationshipType 'HAS_RAID' is defined in more than one Ontology — ReBAC never walks it until the duplicate is removed",
        "ReBAC never walks relationship type(s) LEGACY_LINK: they exist in the KG but no Ontology defines them",
    ]
    # the duplicate HAS_RAID is never walked: PMA reaches their project, not its risks
    assert scope.full == {"ProjectManager": {"pma@example.com"}, "Project": {"P-A"}}
    assert scope.readonly == {}


def test_a_failing_type_listing_never_breaks_the_walk():
    class Failing(GraphSession):
        async def run(self, query, **params):
            if "db.relationshipTypes" in query:
                raise RuntimeError("no procedure access")
            return await super().run(query, **params)

    scope, _ = scope_from(("BS_user", "pma@example.com"), session=Failing())
    assert scope.full["Risk"] == {"R-A"}
    assert scope.unwalked_types == []


def test_reference_depth_must_be_at_least_one():
    with pytest.raises(ValueError):
        scope_from(("BS_user", "pma@example.com"), depth=0)


def test_quoting_in_the_start_pattern():
    assert scope_query("My`Label", "k`ey", "").startswith("MATCH (s:`My``Label` {`k``ey`: $key})")
    assert scope_query("Risk", "id", "").startswith("MATCH (s:`Risk` {`id`: $id})")
    assert rebac.start_param("reference_depth") == "key"


def test_scope_views():
    scope = Scope(full={"Risk": {"R-A"}}, readonly={"Risk": {"R-B"}, "Project": {"P-B"}})
    assert scope.visible == {"Risk": {"R-A", "R-B"}, "Project": {"P-B"}}
    assert scope.writable == {"Risk": {"R-A"}}
    assert scope.sizes() == (1, 2)


def test_load_label_key_map_adds_chunk():
    class Session:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

        async def run(self, query, **params):
            assert query == rebac.LABEL_KEY_MAP_QUERY
            return Result([{"label": "Risk", "key_prop": "id"}, {"label": "Stakeholder", "key_prop": "email"}])

    class Driver:
        def session(self, database=None):
            return Session()

    assert asyncio.run(load_label_key_map(Driver(), "neo4j")) == {"Risk": "id", "Stakeholder": "email", "Chunk": "id"}


@pytest.mark.parametrize("value, expected", [(None, 1), ("2", 2), (" 3 ", 3)])
def test_reference_depth_from_env(monkeypatch, value, expected):
    if value is None:
        monkeypatch.delenv("REBAC_REFERENCE_DEPTH", raising=False)
    else:
        monkeypatch.setenv("REBAC_REFERENCE_DEPTH", value)
    assert rebac.reference_depth_from_env() == expected


@pytest.mark.parametrize("value", ["0", "-1", "two", ""])
def test_reference_depth_from_env_rejects(monkeypatch, value):
    monkeypatch.setenv("REBAC_REFERENCE_DEPTH", value)
    with pytest.raises(ValueError, match="Invalid REBAC_REFERENCE_DEPTH"):
        rebac.reference_depth_from_env()
