import asyncio

import pytest
from neo4j.exceptions import ServiceUnavailable

from bluestar_mcp_shared import neo4j as neo4j_module
from bluestar_mcp_shared import ontology
from bluestar_mcp_shared.ontology import (
    REBAC_SYSTEM_PROPAGATE,
    RELATIONSHIP_TYPES_SUBQUERY,
    RelationshipType,
    build_rebac_filters,
    format_types,
    load_all_relationship_types,
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
        "rebac_out": "propagate",
        "rebac_in": "none",
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


# ---- Task 119: ReBAC per-direction modes ----

SYSTEM = "_CREATED>|_UPDATED>|IS_SAME_PERSON>|IN_DOMAIN>|HAS_CHUNK>|HAS_EXTRACTED_ENTITY>"


def rt(name, rebac_out=None, rebac_in=None):
    return shape_relationship_types([{"name": name, "rebac_out": rebac_out, "rebac_in": rebac_in}])[name]


def test_rebac_modes_default_to_todays_outgoing_walk():
    has_raid = shape_relationship_types(ROWS)["HAS_RAID"]
    assert (has_raid["rebac_out"], has_raid["rebac_in"]) == ("propagate", "none")
    assert (rt("X")["rebac_out"], rt("X")["rebac_in"]) == ("propagate", "none")


def test_rebac_modes_are_read_from_the_row():
    row = {"name": "HAS_RAID", "rebac_out": "propagate", "rebac_in": "reference"}
    has_raid = shape_relationship_types([row])["HAS_RAID"]
    assert (has_raid["rebac_out"], has_raid["rebac_in"]) == ("propagate", "reference")


def test_subquery_reads_the_rebac_properties():
    assert "rebac_out: rt.rebac_out" in RELATIONSHIP_TYPES_SUBQUERY
    assert "rebac_in: rt.rebac_in" in RELATIONSHIP_TYPES_SUBQUERY


def test_filters_with_defaults_are_every_type_outgoing():
    propagate, reference = build_rebac_filters([rt("OWNS"), rt("HAS_RAID")])
    assert propagate == SYSTEM + "|HAS_RAID>|OWNS>"
    assert reference == ""


def test_filters_mix_of_modes_in_both_directions():
    rel_types = [
        rt("HAS_RAID", "propagate", "reference"),
        rt("RELATES_TO", "reference", "none"),
        rt("IS_PM_OF", "propagate", "none"),
        rt("PEER_OF", "propagate", "propagate"),
        rt("CITES", "reference", "reference"),
        rt("BACK_ONLY", "none", "propagate"),
        rt("HIDDEN", "none", "none"),
    ]
    propagate, reference = build_rebac_filters(rel_types)
    assert propagate == SYSTEM + "|<BACK_ONLY|HAS_RAID>|IS_PM_OF>|PEER_OF"
    assert reference == "CITES|<HAS_RAID|RELATES_TO>"


def test_filters_are_sorted_and_accept_a_dict():
    rel_types = {name: rt(name) for name in ("ZETA", "ALPHA", "MID")}
    assert build_rebac_filters(rel_types)[0] == SYSTEM + "|ALPHA>|MID>|ZETA>"


def test_system_types_lead_the_propagate_filter():
    propagate, _ = build_rebac_filters([])
    assert propagate == "|".join(REBAC_SYSTEM_PROPAGATE) == SYSTEM
    assert ">" != propagate and not propagate.endswith("|>")


def test_has_state_and_system_types_are_never_taken_from_the_ontology():
    rel_types = [rt("HAS_STATE", "propagate", "propagate"), rt("_CREATED", "reference", "reference"),
                 rt("IN_DOMAIN", "none", "propagate"), rt("OWNS")]
    propagate, reference = build_rebac_filters(rel_types)
    assert "HAS_STATE" not in propagate and "HAS_STATE" not in reference
    assert propagate == SYSTEM + "|OWNS>"
    assert reference == ""


def test_unknown_mode_counts_as_none():
    propagate, reference = build_rebac_filters([rt("ODD", "everything", "Propagate")])
    assert propagate == SYSTEM
    assert reference == ""


def test_same_name_in_two_ontologies_takes_the_strongest_mode():
    propagate, reference = build_rebac_filters([rt("LINK", "reference", "none"), rt("LINK", "propagate", "reference")])
    assert propagate == SYSTEM + "|LINK>"
    assert reference == "<LINK"


def test_load_all_relationship_types():
    class Result:
        async def data(self):
            return [{"ontology": "BP_Operations", "rel_types": ROWS}, {"ontology": "Empty", "rel_types": []}]

    class Session(FakeSession):
        async def run(self, query, **params):
            self.driver.calls.append((query, params))
            return Result()

    class Driver(FakeDriver):
        def session(self, database=None):
            return Session(self)

    driver = Driver(None)
    loaded = asyncio.run(load_all_relationship_types(driver, "neo4j"))
    assert set(loaded) == {"BP_Operations", "Empty"}
    assert loaded["BP_Operations"]["HAS_RAID"]["from_types"] == {"Project", "Action", "Risk"}
    assert loaded["Empty"] == {}
    assert driver.calls[0][0].startswith("MATCH (o:Ontology)CALL") or "MATCH (o:Ontology)" in driver.calls[0][0]
