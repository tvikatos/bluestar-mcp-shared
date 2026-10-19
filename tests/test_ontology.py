import asyncio

import pytest
from neo4j.exceptions import ServiceUnavailable

from bluestar_mcp_shared import neo4j as neo4j_module
from bluestar_mcp_shared import ontology
from bluestar_mcp_shared.ontology import (
    RELATIONSHIP_TYPES_SUBQUERY,
    RelationshipType,
    format_types,
    load_relationship_type,
    load_relationship_types,
    serializable,
    shape_relationship_types,
)

# rel_types as RELATIONSHIP_TYPES_SUBQUERY returns them (BP_Operations, dev, 2026-10-19)
ROWS = [
    {"name": "HAS_RAID", "cardinality": "MANY", "required": False, "global_required": False,
     "from_types": ["Project", "Action", "Risk"], "to_types": ["Action", "Risk"], "properties": []},
    {"name": "HAS_OWNER", "cardinality": "MANY", "required": False, "global_required": False,
     "from_types": ["Action", "Risk"], "to_types": ["Stakeholder"],
     "properties": [
         {"name": "fromDate", "type": "DATE", "required": False, "enum_values": None},
         {"name": "toDate", "type": "DATE", "required": False, "enum_values": None},
     ]},
    {"name": "RAID_IN_PROJECT", "cardinality": "ONE", "required": True, "global_required": False,
     "from_types": ["Action", "Risk"], "to_types": ["Project"], "properties": []},
    {"name": "MENTIONS", "cardinality": None, "required": None, "global_required": True,
     "from_types": [], "to_types": ["Stakeholder"], "properties": [None]},
]


class FakeResult:
    def __init__(self, record):
        self.record = record

    async def single(self):
        return self.record


class FakeSession:
    def __init__(self, driver):
        self.driver = driver

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def run(self, query, **params):
        self.driver.calls.append((query, params))
        if self.driver.errors:
            raise self.driver.errors.pop(0)
        return FakeResult(self.driver.record)


class FakeDriver:
    def __init__(self, record, errors=()):
        self.record = record
        self.errors = list(errors)
        self.calls = []
        self.databases = []

    def session(self, database=None):
        self.databases.append(database)
        return FakeSession(self)


@pytest.fixture
def no_sleep(monkeypatch):
    async def fake_sleep(delay):
        pass

    monkeypatch.setattr(neo4j_module.asyncio, "sleep", fake_sleep)


def test_shape_uses_sets_for_every_endpoint():
    rel_types = shape_relationship_types(ROWS)
    assert rel_types["HAS_RAID"]["from_types"] == {"Project", "Action", "Risk"}
    assert rel_types["HAS_RAID"]["to_types"] == {"Action", "Risk"}
    assert rel_types["HAS_OWNER"]["from_types"] == {"Action", "Risk"}
    assert rel_types["HAS_OWNER"]["to_types"] == {"Stakeholder"}


def test_shape_fields():
    assert shape_relationship_types(ROWS)["HAS_OWNER"] == {
        "name": "HAS_OWNER",
        "cardinality": "MANY",
        "required": False,
        "global_required": False,
        "from_types": {"Action", "Risk"},
        "to_types": {"Stakeholder"},
        "properties": {
            "fromDate": {"name": "fromDate", "type": "DATE", "required": False, "enum_values": None},
            "toDate": {"name": "toDate", "type": "DATE", "required": False, "enum_values": None},
        },
    }


def test_shape_defaults_for_missing_values():
    mentions = shape_relationship_types(ROWS)["MENTIONS"]
    assert mentions["from_types"] == set()
    assert mentions["required"] is False
    assert mentions["global_required"] is True
    assert mentions["cardinality"] is None
    assert mentions["properties"] == {}


def test_shape_drops_null_endpoint_names_and_handles_no_rows():
    row = {"name": "X", "from_types": [None, "A"], "to_types": None}
    assert shape_relationship_types([row])["X"]["from_types"] == {"A"}
    assert shape_relationship_types([row])["X"]["to_types"] == set()
    assert shape_relationship_types(None) == {}
    assert shape_relationship_types([]) == {}


def test_subquery_collects_from_and_to_separately():
    # One subquery per edge type, each with collect(DISTINCT ...): matching FROM
    # and TO together is what produced one row per pairing.
    assert "collect(DISTINCT f.name) AS from_types" in RELATIONSHIP_TYPES_SUBQUERY
    assert "collect(DISTINCT t.name) AS to_types" in RELATIONSHIP_TYPES_SUBQUERY
    assert RELATIONSHIP_TYPES_SUBQUERY.count("CALL {") == 4
    assert "AS rel_types" in RELATIONSHIP_TYPES_SUBQUERY


def test_load_relationship_types():
    driver = FakeDriver({"rel_types": ROWS})
    rel_types = asyncio.run(load_relationship_types(driver, "ontology", "BP_Operations"))
    assert set(rel_types) == {"HAS_RAID", "HAS_OWNER", "RAID_IN_PROJECT", "MENTIONS"}
    assert rel_types["RAID_IN_PROJECT"]["required"] is True
    query, params = driver.calls[0]
    assert params == {"ontology_name": "BP_Operations"}
    assert query.startswith("MATCH (o:Ontology {name: $ontology_name})")
    assert RELATIONSHIP_TYPES_SUBQUERY in query
    assert driver.databases == ["ontology"]


def test_load_relationship_types_unknown_ontology():
    assert asyncio.run(load_relationship_types(FakeDriver(None), "neo4j", "Nope")) == {}


def test_load_relationship_type():
    driver = FakeDriver({"rel_types": ROWS})
    has_raid = asyncio.run(load_relationship_type(driver, "neo4j", "BP_Operations", "HAS_RAID"))
    assert has_raid["from_types"] == {"Project", "Action", "Risk"}
    assert asyncio.run(load_relationship_type(driver, "neo4j", "BP_Operations", "NOPE")) is None


def test_load_is_retried_on_a_dropped_connection(no_sleep):
    driver = FakeDriver({"rel_types": ROWS}, errors=[ServiceUnavailable("dropped")])
    rel_types = asyncio.run(load_relationship_types(driver, "neo4j", "BP_Operations"))
    assert "HAS_RAID" in rel_types
    assert len(driver.calls) == 2


def test_format_types_is_sorted():
    assert format_types({"Project", "Action", "Risk"}) == "Action, Project, Risk"
    assert format_types({"Stakeholder"}) == "Stakeholder"
    assert format_types(set()) == ""


def test_module_exports():
    assert ontology.shape_relationship_type(ROWS[0])["name"] == "HAS_RAID"


def test_definitions_are_relationship_types_and_plain_dicts():
    has_raid = shape_relationship_types(ROWS)["HAS_RAID"]
    assert isinstance(has_raid, RelationshipType) and isinstance(has_raid, dict)
    assert has_raid["cardinality"] == "MANY"


def test_endpoint_rule_checks_each_end_on_its_own():
    rel_types = shape_relationship_types(ROWS)
    has_raid, has_owner = rel_types["HAS_RAID"], rel_types["HAS_OWNER"]
    # any FROM x any TO: no pairings
    assert has_raid.allows_from("Project") and has_raid.allows_to("Risk")
    assert has_raid.allows_from("Action") and has_raid.allows_to("Action")
    assert not has_raid.allows_from("Stakeholder")
    assert not has_raid.allows_to("Project")
    # the validate_graph check 24 case
    assert has_owner.allows_from("Action") and has_owner.allows_to("Stakeholder")
    assert not has_owner.allows_to("Risk")


def test_empty_from_types_allows_any_source():
    mentions = shape_relationship_types(ROWS)["MENTIONS"]
    assert mentions["from_types"] == set()
    assert mentions.allows_from("Project")
    assert mentions.allows_from("Anything")


def test_empty_to_types_allows_no_target():
    broken = shape_relationship_types([{"name": "BROKEN", "from_types": ["A"], "to_types": []}])["BROKEN"]
    assert not broken.allows_to("A")
    assert not broken.allows_to("Anything")


def test_serializable_sorts_the_sets_without_changing_the_definition():
    import json
    has_raid = shape_relationship_types(ROWS)["HAS_RAID"]
    out = serializable(has_raid)
    assert out["from_types"] == ["Action", "Project", "Risk"]
    assert out["to_types"] == ["Action", "Risk"]
    assert json.loads(json.dumps(out))["name"] == "HAS_RAID"
    assert has_raid["from_types"] == {"Project", "Action", "Risk"}
