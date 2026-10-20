"""
Bluestar MCP Shared Ontology-db RelationshipType Definitions

A RelationshipType is one node with any number of FROM and TO edges to
NodeTypes. This module is the single source of truth for reading one: every
server loads RelationshipTypes in the same set-based shape, so no code path
can read just the first FROM or TO it finds.

    {
        "name": str,
        "cardinality": "ONE" | "MANY" | None,
        "required": bool,
        "global_required": bool,     # (:Ontology)-[:HAS_RELATIONSHIP]->(rt)
        "from_types": set[str],      # every (rt)-[:FROM]->(:NodeType)
        "to_types": set[str],        # every (rt)-[:TO]->(:NodeType)
        "properties": {name: {name, type, required, enum_values}},
        "rebac_out": "propagate" | "reference" | "none",   # default "propagate"
        "rebac_in": "propagate" | "reference" | "none",    # default "none"
    }

- RELATIONSHIP_TYPES_SUBQUERY: Cypher CALL subquery, with `o` (the Ontology)
  in scope, returning `rel_types`; embed it in a larger query (the servers'
  ontology bundle) to keep a single round trip
- RelationshipType: the definition (a dict), with allows_from / allows_to,
  the one place the endpoint rule lives
- shape_relationship_types: rows of that subquery -> {name: definition}
- load_relationship_types / load_relationship_type: run it on their own
- format_types: an endpoint set for error messages
- build_rebac_filters: the APOC relationship filters for ReBAC's two-tier
  scope walk (Task 119), the one place they are built
"""
from .neo4j import with_retry

# FROM, TO and HAS_PROPERTY are collected in separate subqueries: matching
# them together multiplies the rows (one per FROM x TO pairing), which is
# how a RelationshipType ended up read as a single FROM/TO pair.
RELATIONSHIP_TYPES_SUBQUERY = """
CALL {
    WITH o
    MATCH (o)-[:CONTAINS]->(rt:RelationshipType)
    CALL {
        WITH rt
        OPTIONAL MATCH (rt)-[:FROM]->(f:NodeType)
        RETURN collect(DISTINCT f.name) AS from_types
    }
    CALL {
        WITH rt
        OPTIONAL MATCH (rt)-[:TO]->(t:NodeType)
        RETURN collect(DISTINCT t.name) AS to_types
    }
    CALL {
        WITH rt
        OPTIONAL MATCH (rt)-[rp:HAS_PROPERTY]->(pd:PropertyDef)
        RETURN collect(CASE WHEN pd IS NULL THEN null ELSE {
            name: pd.name, type: pd.type, required: coalesce(rp.required, false), enum_values: pd.enum_values
        } END) AS properties
    }
    RETURN collect({
        name: rt.name,
        cardinality: rt.cardinality,
        required: coalesce(rt.required, false),
        global_required: EXISTS { (o)-[:HAS_RELATIONSHIP]->(rt) },
        from_types: from_types,
        to_types: to_types,
        properties: properties,
        rebac_out: rt.rebac_out,
        rebac_in: rt.rebac_in
    }) AS rel_types
}
"""

_LOAD_QUERY = "MATCH (o:Ontology {name: $ontology_name})" + RELATIONSHIP_TYPES_SUBQUERY + "RETURN rel_types"
_LOAD_ALL_QUERY = (
    "/* load_all_relationship_types */ MATCH (ontology:Ontology) WITH ontology AS o"
    + RELATIONSHIP_TYPES_SUBQUERY
    + "RETURN o.name AS ontology, rel_types"
)

# Task 119 — how ReBAC's scope walk crosses a relationship of this type, per
# graph direction (out: FROM -> TO, in: TO -> FROM):
#   propagate: the node reached is in full scope and the walk continues from it
#   reference: the node reached is in read-only scope; the walk stops there
#   none:      the walk never crosses it in that direction
# The defaults reproduce the single outgoing walk ReBAC had before.
REBAC_MODES = ("propagate", "reference", "none")
REBAC_OUT_DEFAULT = "propagate"
REBAC_IN_DEFAULT = "none"

# System relationships the propagate walk always follows, outgoing, ahead of
# the ontology's own types: provenance, person and domain links, and the
# ingested-content chain (user -_CREATED-> document -HAS_CHUNK-> chunk
# -HAS_EXTRACTED_ENTITY-> entity), which the old bare '>' walk followed too.
REBAC_SYSTEM_PROPAGATE = (
    "_CREATED>", "_UPDATED>", "IS_SAME_PERSON>", "IN_DOMAIN>", "HAS_CHUNK>", "HAS_EXTRACTED_ENTITY>",
)
# Never taken from the ontology: the system types above are fixed in code,
# and HAS_STATE is never walked (State is always visible anyway).
REBAC_EXCLUDED_TYPES = frozenset({
    "HAS_STATE", "_CREATED", "_UPDATED", "IS_SAME_PERSON", "IN_DOMAIN", "HAS_CHUNK", "HAS_EXTRACTED_ENTITY",
})


class RelationshipType(dict):
    """
    A RelationshipType definition: a plain dict in the shape above, plus the
    endpoint rule. A relationship is valid if its source label is in
    from_types AND its target label is in to_types, each end checked on its
    own (no FROM/TO pairings).
    """

    def allows_from(self, label: str) -> bool:
        """
        Whether a relationship of this type may start at `label`. An empty
        from_types allows any label: an ontology-level (HAS_RELATIONSHIP)
        type needs no FROM edge.
        """
        return not self["from_types"] or label in self["from_types"]

    def allows_to(self, label: str) -> bool:
        """
        Whether a relationship of this type may end at `label`. Unlike FROM,
        an empty to_types allows nothing: a RelationshipType with no TO edge
        is a broken definition (validate_ontology reports it).
        """
        return label in self["to_types"]


def shape_relationship_type(row: dict) -> RelationshipType:
    """One row of RELATIONSHIP_TYPES_SUBQUERY's rel_types -> the set-based definition."""
    return RelationshipType({
        "name": row["name"],
        "cardinality": row.get("cardinality"),
        "required": bool(row.get("required")),
        "global_required": bool(row.get("global_required")),
        "from_types": {name for name in (row.get("from_types") or []) if name is not None},
        "to_types": {name for name in (row.get("to_types") or []) if name is not None},
        "properties": {p["name"]: p for p in (row.get("properties") or []) if p is not None},
        # Absent -> default. A value outside REBAC_MODES is kept as is, for
        # validate_ontology to report; build_rebac_filters treats it as none.
        "rebac_out": row.get("rebac_out") or REBAC_OUT_DEFAULT,
        "rebac_in": row.get("rebac_in") or REBAC_IN_DEFAULT,
    })


def shape_relationship_types(rows) -> dict[str, RelationshipType]:
    """RELATIONSHIP_TYPES_SUBQUERY's rel_types -> {name: definition}."""
    return {row["name"]: shape_relationship_type(row) for row in (rows or [])}


async def load_relationship_types(driver, database: str, ontology_name: str) -> dict[str, RelationshipType]:
    """Every RelationshipType the Ontology CONTAINS, as {name: definition}; {} if it doesn't exist."""
    async def _read():
        async with driver.session(database=database) as session:
            result = await session.run(_LOAD_QUERY, ontology_name=ontology_name)
            return await result.single()

    record = await with_retry(_read)
    return shape_relationship_types(record["rel_types"] if record else [])


async def load_all_relationship_types(driver, database: str) -> dict[str, dict[str, RelationshipType]]:
    """Every Ontology's RelationshipTypes, as {ontology_name: {name: definition}}."""
    async def _read():
        async with driver.session(database=database) as session:
            result = await session.run(_LOAD_ALL_QUERY)
            return await result.data()

    rows = await with_retry(_read)
    return {row["ontology"]: shape_relationship_types(row["rel_types"]) for row in rows}


async def load_relationship_type(
    driver, database: str, ontology_name: str, name: str
) -> RelationshipType | None:
    """One RelationshipType of the Ontology by name, or None."""
    return (await load_relationship_types(driver, database, ontology_name)).get(name)


def serializable(rel_type: dict) -> dict:
    """A definition for JSON output: from_types/to_types as sorted lists."""
    return {
        **rel_type,
        "from_types": sorted(rel_type["from_types"]),
        "to_types": sorted(rel_type["to_types"]),
    }


def format_types(types) -> str:
    """An endpoint set for messages, sorted so the text is stable: 'Action, Risk'."""
    return ", ".join(sorted(types))


_REBAC_STRENGTH = {"none": 0, "reference": 1, "propagate": 2}


def _rebac_mode(value) -> str:
    """A rebac_out/rebac_in value; anything outside REBAC_MODES counts as none (fail closed)."""
    return value if value in REBAC_MODES else "none"


def build_rebac_filters(rel_types) -> tuple[str, str]:
    """
    (propagate_filter, reference_filter): APOC relationshipFilter strings for
    ReBAC's two scope walks, from RelationshipType definitions (an iterable,
    or a {name: definition} dict).

    Each type is listed explicitly, sorted by name, in APOC's [<]TYPE[>]
    syntax: out only -> TYPE>, in only -> <TYPE, both -> TYPE. Never a bare
    '>'. The propagate filter starts with REBAC_SYSTEM_PROPAGATE; HAS_STATE
    and the system types are never taken from the ontology. A name defined in
    several Ontologies gets the strongest mode of any definition, per
    direction (propagate > reference > none).

    The reference filter is "" when no type references in either direction.
    An empty relationshipFilter means EVERY type to APOC, so callers must skip
    the reference walk then, never pass "".
    """
    if isinstance(rel_types, dict):
        rel_types = rel_types.values()
    modes: dict[str, list[str]] = {}
    for rt in rel_types:
        name = rt["name"]
        if name in REBAC_EXCLUDED_TYPES:
            continue
        out_mode = _rebac_mode(rt.get("rebac_out", REBAC_OUT_DEFAULT))
        in_mode = _rebac_mode(rt.get("rebac_in", REBAC_IN_DEFAULT))
        current = modes.setdefault(name, ["none", "none"])
        if _REBAC_STRENGTH[out_mode] > _REBAC_STRENGTH[current[0]]:
            current[0] = out_mode
        if _REBAC_STRENGTH[in_mode] > _REBAC_STRENGTH[current[1]]:
            current[1] = in_mode

    def entries(tier: str) -> list[str]:
        out = []
        for name in sorted(modes):
            out_mode, in_mode = modes[name]
            if out_mode == tier and in_mode == tier:
                out.append(name)
            elif out_mode == tier:
                out.append(f"{name}>")
            elif in_mode == tier:
                out.append(f"<{name}")
        return out

    propagate = "|".join([*REBAC_SYSTEM_PROPAGATE, *entries("propagate")])
    reference = "|".join(entries("reference"))
    return propagate, reference
