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
        "from_types": set[str],      # every (rt)-[:FROM]->(:NodeType|:ExternalDataType)
        "to_types": set[str],        # every (rt)-[:TO]->(:NodeType|:ExternalDataType)
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
- ExternalDataType (Task 136): load_external_data_types, is_collection /
  is_collection_of (used by allows_from / allows_to), the built-in instance
  properties and external_data_bundle
"""
import logging

from .neo4j import with_retry

logger = logging.getLogger(__name__)

# FROM, TO and HAS_PROPERTY are collected in separate subqueries: matching
# them together multiplies the rows (one per FROM x TO pairing), which is
# how a RelationshipType ended up read as a single FROM/TO pair.
RELATIONSHIP_TYPES_SUBQUERY = """
CALL {
    WITH o
    MATCH (o)-[:CONTAINS]->(rt:RelationshipType)
    CALL {
        WITH rt
        OPTIONAL MATCH (rt)-[:FROM]->(f) WHERE f:NodeType OR f:ExternalDataType
        RETURN collect(DISTINCT f.name) AS from_types
    }
    CALL {
        WITH rt
        OPTIONAL MATCH (rt)-[:TO]->(t) WHERE t:NodeType OR t:ExternalDataType
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

    def allows_from(self, label: str, external_data_types: dict | None = None) -> bool:
        """
        Whether a relationship of this type may start at `label`. An empty
        from_types allows any label: an ontology-level (HAS_RELATIONSHIP)
        type needs no FROM edge. Task 136: an ExternalDataType that is a
        collection of an allowed type is allowed too (pass the loaded
        external_data_types).
        """
        return not self["from_types"] or _allowed(label, self["from_types"], external_data_types)

    def allows_to(self, label: str, external_data_types: dict | None = None) -> bool:
        """
        Whether a relationship of this type may end at `label`, or at a
        collection of an allowed type. Unlike FROM, an empty to_types allows
        nothing: a RelationshipType with no TO edge is a broken definition
        (validate_ontology reports it).
        """
        return _allowed(label, self["to_types"], external_data_types)


def _allowed(label: str, types, external_data_types: dict | None) -> bool:
    return label in types or any(is_collection_of(label, t, external_data_types) for t in types)


# ---- Task 136: ExternalDataType ----
#
# An ExternalDataType (e.g. RiskRegister) points at a user's own sheet: the KG
# holds a pointer node plus its semantics, and the agent reads the sheet live.
# Like DocType it is a standalone Ontology-db definition (not CONTAINS'd by an
# Ontology), never instantiated itself; KG nodes carry its name as their label.
# (:ExternalDataType)-[:IS_COLLECTION_OF]->(:NodeType): an instance stands in
# for those NodeTypes at either end of their RelationshipTypes, without their
# cardinality or required rules.
#
#     {"name": str, "description": str | None, "policy": str | None,
#      "collection_of": set[str]}

EXTERNAL_DATA_TYPES_QUERY = """
MATCH (et:ExternalDataType)
OPTIONAL MATCH (et)-[:IS_COLLECTION_OF]->(nt:NodeType)
RETURN et.name AS name, et.description AS description, et.policy AS policy,
       collect(DISTINCT nt.name) AS collection_of
"""


def shape_external_data_types(rows) -> dict[str, dict]:
    """EXTERNAL_DATA_TYPES_QUERY rows -> {name: definition}."""
    return {
        row["name"]: {
            "name": row["name"],
            "description": row.get("description"),
            "policy": row.get("policy"),
            "collection_of": {n for n in (row.get("collection_of") or []) if n is not None},
        }
        for row in (rows or []) if row.get("name") is not None
    }


async def load_external_data_types(driver, database: str) -> dict[str, dict]:
    """Every ExternalDataType in the Ontology-db, as {name: definition} (with retry)."""
    async def _read():
        async with driver.session(database=database) as session:
            result = await session.run(EXTERNAL_DATA_TYPES_QUERY)
            return await result.data()

    return shape_external_data_types(await with_retry(_read))


def is_collection(label: str, external_data_types: dict | None) -> bool:
    """Whether `label` is an ExternalDataType — a collection, so cardinality and
    required relationship rules don't apply to its instances."""
    return bool(external_data_types) and label in external_data_types


def is_collection_of(label: str, node_type: str, external_data_types: dict | None) -> bool:
    """Whether `label` is an ExternalDataType that IS_COLLECTION_OF `node_type`."""
    return is_collection(label, external_data_types) and node_type in external_data_types[label]["collection_of"]


# Built-in properties of every ExternalDataType instance (no per-type
# PropertyDefs in v1), in the same shape as an ontology bundle's prop_defs, so
# the usual property checks and coercion apply unchanged.
EXTERNAL_DATA_KEY_PROP = "id"


def _built_in(name, type_, required=False, is_key=False, generated=False):
    return {"name": name, "type": type_, "is_key": is_key, "generated": generated,
            "required": required, "enum_values": None, "default_value": None}


EXTERNAL_DATA_PROP_DEFS = {
    "id": _built_in("id", "STRING", required=True, is_key=True, generated=True),
    "url": _built_in("url", "STRING", required=True),
    "description": _built_in("description", "STRING"),
    "read_rules": _built_in("read_rules", "STRING"),
    "last_scanned_at": _built_in("last_scanned_at", "DATETIME"),
}


def external_data_url_error(prop_name: str, value) -> str | None:
    """An instance's url must be an http(s) URL (no host check in v1); the error, or None."""
    if isinstance(value, str) and value.strip().lower().startswith(("http://", "https://")) \
            and len(value.strip()) > len("https://"):
        return None
    return f"Property '{prop_name}' must be an http(s) URL, got {type(value).__name__} ('{value}')"


def check_external_data_properties(properties: dict) -> str | None:
    """
    The built-in property checks for an ExternalDataType instance's incoming
    values: declared types (coercing in place, e.g. a last_scanned_at ISO
    string to an aware UTC datetime, via the shared property_types rules) and
    an http(s) url. Required and unknown properties are the caller's usual
    bundle checks against EXTERNAL_DATA_PROP_DEFS. Returns the first error,
    or None.
    """
    from .property_types import coerce_and_check_property_types

    error = coerce_and_check_property_types(properties, EXTERNAL_DATA_PROP_DEFS)
    if error:
        return error
    if properties.get("url") is not None:
        return external_data_url_error("url", properties["url"])
    return None


def external_data_bundle(external_data_type: dict, rel_types: dict) -> dict:
    """
    An ontology-bundle-shaped definition for an ExternalDataType label, so a
    server's write path can treat its instances like any node: the built-in
    prop_defs, a generated `id` key, no lifecycle, and the ontology's
    RelationshipTypes (endpoint checks go through allows_from / allows_to with
    the external data types).
    """
    return {
        "node_type_exists": True,
        "is_external_data_type": True,
        "external_data_type": external_data_type,
        "prop_defs": {name: dict(pd) for name, pd in EXTERNAL_DATA_PROP_DEFS.items()},
        "key_prop": EXTERNAL_DATA_KEY_PROP,
        "generated_key": True,
        "lifecycle_id": None,
        "states": [],
        "transitions": [],
        "rel_types": rel_types,
    }


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


def _rebac_mode(value) -> str:
    """A rebac_out/rebac_in value; anything outside REBAC_MODES counts as none (fail closed)."""
    return value if value in REBAC_MODES else "none"


def resolve_rebac_modes(rel_types) -> tuple[dict[str, tuple[str, str]], list[str]]:
    """
    ({name: (out_mode, in_mode)}, warnings) for RelationshipType definitions
    (an iterable, or a {name: definition} dict): the mode ReBAC actually uses
    for each ontology type, per direction. HAS_STATE and the system types are
    skipped (they are fixed in code, see REBAC_SYSTEM_PROPAGATE).

    Fail closed: a value outside REBAC_MODES counts as none, and a name
    defined in more than one Ontology (names are unique across the
    Ontology-db; validate_ontology reports a duplicate) is never walked. Each
    case adds a warning; nothing is logged here.
    """
    if isinstance(rel_types, dict):
        rel_types = rel_types.values()
    modes: dict[str, tuple[str, str]] = {}
    duplicates: set[str] = set()
    warnings: list[str] = []
    for rt in rel_types:
        name = rt["name"]
        if name in REBAC_EXCLUDED_TYPES:
            continue
        if name in modes:
            duplicates.add(name)
            continue
        out_mode, in_mode = rt.get("rebac_out", REBAC_OUT_DEFAULT), rt.get("rebac_in", REBAC_IN_DEFAULT)
        for prop, value in (("rebac_out", out_mode), ("rebac_in", in_mode)):
            if value not in REBAC_MODES:
                warnings.append(
                    f"RelationshipType '{name}' has {prop} = {value!r}, not one of "
                    f"{', '.join(REBAC_MODES)} — ReBAC treats it as none"
                )
        modes[name] = (_rebac_mode(out_mode), _rebac_mode(in_mode))
    for name in sorted(duplicates):
        warnings.append(
            f"RelationshipType '{name}' is defined in more than one Ontology — ReBAC never walks it "
            f"until the duplicate is removed"
        )
        modes[name] = ("none", "none")
    return modes, warnings


def rebac_filters_from_modes(modes: dict[str, tuple[str, str]]) -> tuple[str, str]:
    """(propagate_filter, reference_filter) for the modes resolve_rebac_modes returns."""
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


def build_rebac_filters(rel_types) -> tuple[str, str]:
    """
    (propagate_filter, reference_filter): APOC relationshipFilter strings for
    ReBAC's two scope walks, from RelationshipType definitions (an iterable,
    or a {name: definition} dict).

    Each type is listed explicitly, sorted by name, in APOC's [<]TYPE[>]
    syntax: out only -> TYPE>, in only -> <TYPE, both -> TYPE. Never a bare
    '>'. The propagate filter starts with REBAC_SYSTEM_PROPAGATE; HAS_STATE
    and the system types are never taken from the ontology. The rules for
    each type's mode are resolve_rebac_modes'; its warnings are logged.

    The reference filter is "" when no type references in either direction.
    An empty relationshipFilter means EVERY type to APOC, so callers must skip
    the reference walk then, never pass "".
    """
    modes, warnings = resolve_rebac_modes(rel_types)
    for warning in warnings:
        logger.warning(warning)
    return rebac_filters_from_modes(modes)
