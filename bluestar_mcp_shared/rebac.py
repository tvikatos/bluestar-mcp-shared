"""
Bluestar MCP Shared ReBAC Scope Computation

The one implementation of the two-tier ReBAC scope walk (Task 119), used by
mcp-BP-KG to enforce scope and by mcp-kg-manager's explain_scope to audit it,
so the audit always reports exactly what is enforced.

From a start node (any KG node, by label + key):
1. Propagate walk over the propagate filter: everything reached is FULL scope.
2. Reference hop from the start and every full-scope node over the reference
   filter, up to reference_depth hops: everything reached that isn't already
   full scope is READ-ONLY scope.
The filters come from each RelationshipType's rebac_out / rebac_in
(bluestar_mcp_shared.ontology). Both walks run on the caller's session, so
the role's RBAC bounds them: a type or label it can't TRAVERSE is a wall, and
a node whose key it can't READ comes back with a null key and isn't collected.

- compute_scope: the walk, from a session, a start node, the RelationshipTypes
  and the label -> key-property map
- Scope: full and readonly ({label: set(keys)}), visible / writable, the
  filters used and the warnings met
- load_label_key_map: the label -> key-property map from the Ontology-db
- SYSTEM_LABELS: labels never collected into scope (always visible)
- reference_depth_from_env: REBAC_REFERENCE_DEPTH, validated the same way
  for every server
"""
import os
import re

from .neo4j import with_retry
from .ontology import REBAC_EXCLUDED_TYPES, rebac_filters_from_modes, resolve_rebac_modes

# Never filtered by $scope, never targeted by a scope check, never collected
# into a scope set — their visibility is bounded only by RBAC. None of them
# are ontology-defined NodeTypes, so none has an is_key property.
SYSTEM_LABELS = frozenset({"State", "BS_user", "Domain", "Sequence", "ReBACScopeVersion"})

# Key properties of the system labels a walk can start from (not in the
# ontology's label -> key map).
SYSTEM_KEY_PROPS = {"BS_user": "email"}

ScopeMap = dict[str, set]


def reference_depth_from_env() -> int:
    """REBAC_REFERENCE_DEPTH (default 1): how many hops the reference walk may
    take. Raises ValueError unless it is an integer >= 1, so a bad value fails
    startup."""
    raw = os.environ.get("REBAC_REFERENCE_DEPTH", "1").strip()
    try:
        depth = int(raw)
    except ValueError:
        depth = 0
    if depth < 1:
        raise ValueError(f"Invalid REBAC_REFERENCE_DEPTH '{raw}' — must be an integer >= 1")
    return depth


class Scope:
    """
    A two-tier ReBAC scope: {label: set(keys)} for full scope and for
    read-only scope (never overlapping: a node reached both ways is full),
    plus the relationship filters the walk used and the warnings it met
    (definitions it had to fail closed on, KG types no Ontology defines).
    """

    def __init__(self, full: ScopeMap | None = None, readonly: ScopeMap | None = None,
                 filters: tuple[str, str] | None = None, warnings: list[str] | None = None,
                 unwalked_types: list[str] | None = None):
        self.full: ScopeMap = full or {}
        self.readonly: ScopeMap = readonly or {}
        self.filters: tuple[str, str] | None = filters
        self.warnings: list[str] = warnings or []
        self.unwalked_types: list[str] = unwalked_types or []

    @property
    def visible(self) -> ScopeMap:
        """Full and read-only scope together — what reads are filtered by."""
        merged = {label: set(keys) for label, keys in self.full.items()}
        for label, keys in self.readonly.items():
            merged.setdefault(label, set()).update(keys)
        return merged

    @property
    def writable(self) -> ScopeMap:
        """Full scope only — what a write to an existing node requires."""
        return self.full

    def sizes(self) -> tuple[int, int]:
        """(full, readonly) node-key counts."""
        return sum(map(len, self.full.values())), sum(map(len, self.readonly.values()))

    def __eq__(self, other) -> bool:
        return isinstance(other, Scope) and (self.full, self.readonly) == (other.full, other.readonly)

    def __repr__(self) -> str:
        return f"Scope(full={self.full!r}, readonly={self.readonly!r})"


def quote_ident(identifier: str) -> str:
    """A Cypher identifier in backticks, with embedded backticks doubled."""
    return "`" + str(identifier).replace("`", "``") + "`"


_WALK_PARAMS = {"propagate_filter", "reference_filter", "reference_depth"}


def start_param(key_prop: str) -> str:
    """The query parameter holding the start key: named after the key property
    ($email for a BS_user) when that is a plain identifier, else $key."""
    if re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", key_prop) and key_prop not in _WALK_PARAMS:
        return key_prop
    return "key"


def start_pattern(label: str, key_prop: str) -> str:
    """The MATCH pattern for a start node `s` by label and key (bound to
    start_param(key_prop)). An inactive BS_user starts nothing (an empty
    scope), as before."""
    return (
        f"MATCH (s:{quote_ident(label)} {{{quote_ident(key_prop)}: ${start_param(key_prop)}}}) "
        f"WHERE NOT s:BS_user OR coalesce(s.is_active, true) "
    )


# Two walks in one round trip. Each runs in its own CALL subquery returning a
# collected list, so an empty walk still yields one row instead of dropping it.
_PROPAGATE_WALK = (
    "CALL { WITH s "
    "CALL apoc.path.subgraphNodes(s, {relationshipFilter: $propagate_filter, minLevel: 1}) YIELD node "
    "RETURN collect(node) AS full_nodes } "
)
_REFERENCE_WALK = (
    "CALL { WITH s, full_nodes "
    "CALL apoc.path.subgraphNodes([s] + full_nodes, "
    "{relationshipFilter: $reference_filter, minLevel: 1, maxLevel: $reference_depth}) YIELD node "
    "RETURN collect(node) AS reached } "
    "WITH full_nodes, [n IN reached WHERE NOT n IN full_nodes] AS readonly_nodes "
)
_SCOPE_ROWS = (
    "UNWIND [n IN full_nodes | {n: n, tier: 'full'}] + [n IN readonly_nodes | {n: n, tier: 'readonly'}] AS row "
    "RETURN labels(row.n) AS labels, row.n AS n, row.tier AS tier"
)


def scope_query(label: str, key_prop: str, reference_filter: str) -> str:
    """The scope query from a start node; without the reference walk when
    nothing references (an empty relationshipFilter means every type to APOC)."""
    walks = _PROPAGATE_WALK + (_REFERENCE_WALK if reference_filter else "WITH full_nodes, [] AS readonly_nodes ")
    return start_pattern(label, key_prop) + walks + _SCOPE_ROWS


def start_key_prop(label: str, key_map: dict) -> str | None:
    """The key property of a start label: the ontology's, or a system label's."""
    return key_map.get(label) or SYSTEM_KEY_PROPS.get(label)


async def _unwalked_types(session, rel_types) -> list[str]:
    """
    Relationship types the KG (as this session sees it) holds that no
    Ontology defines and that aren't system types: the walk lists every type
    it follows, so their edges are never walked. A diagnostic only — never
    fails the walk.
    """
    try:
        result = await session.run("CALL db.relationshipTypes() YIELD relationshipType RETURN relationshipType")
        kg_types = {row["relationshipType"] for row in await result.data()}
    except Exception:  # noqa: BLE001 — the diagnostic must never break scope
        return []
    names = {rt["name"] for rt in (rel_types.values() if isinstance(rel_types, dict) else rel_types)}
    return sorted(kg_types - names - REBAC_EXCLUDED_TYPES)


def unwalked_warning(types) -> str:
    return (
        f"ReBAC never walks relationship type(s) {', '.join(types)}: they exist in the KG "
        f"but no Ontology defines them"
    )


async def compute_scope(session, start: tuple[str, object], rel_types, key_map: dict, *,
                        reference_depth: int = 1) -> Scope:
    """
    The two-tier scope from start = (label, key), on `session` (whose account
    and role bound the walk). rel_types: every Ontology's RelationshipType
    definitions (bluestar_mcp_shared.ontology); key_map: {label: key_prop}
    (load_label_key_map). An unknown start label, or a start node that doesn't
    exist or isn't visible on this session, gives an empty scope.

    Nodes of system labels, of labels with no known key, or whose key is null
    (missing, or not readable by the role) aren't collected. Full wins over
    read-only, by node and by key.
    """
    if reference_depth < 1:
        raise ValueError(f"reference_depth must be >= 1, got {reference_depth}")
    label, key = start
    modes, warnings = resolve_rebac_modes(rel_types)
    propagate_filter, reference_filter = rebac_filters_from_modes(modes)
    scope = Scope(filters=(propagate_filter, reference_filter), warnings=list(warnings))
    key_prop = start_key_prop(label, key_map)
    if key_prop is None:
        scope.warnings.append(f"Start label '{label}' has no known key property — nothing to walk from")
        return scope

    result = await session.run(
        scope_query(label, key_prop, reference_filter),
        **{start_param(key_prop): key},
        propagate_filter=propagate_filter,
        reference_filter=reference_filter,
        reference_depth=reference_depth,
    )
    rows = await result.data()
    scope.unwalked_types = await _unwalked_types(session, rel_types)
    if scope.unwalked_types:
        scope.warnings.append(unwalked_warning(scope.unwalked_types))

    for row in rows:
        tier = scope.readonly if row.get("tier") == "readonly" else scope.full
        node_props = dict(row["n"])
        for node_label in row["labels"]:
            if node_label in SYSTEM_LABELS:
                continue
            node_key_prop = key_map.get(node_label)
            if not node_key_prop:
                continue
            key_val = node_props.get(node_key_prop)
            if key_val is None:
                continue
            tier.setdefault(node_label, set()).add(key_val)

    # Full wins: the query already keeps the tiers apart by node, but two nodes
    # can share a label + key (e.g. a stale duplicate), so check by key as well.
    for node_label, keys in list(scope.readonly.items()):
        keys -= scope.full.get(node_label, set())
        if not keys:
            del scope.readonly[node_label]
    return scope


# {label: key_prop} for every NodeType with an is_key HAS_PROPERTY, plus 'id'
# for every TextType and DocType; the reserved system label Chunk is added in
# code ('id' too), since it is never modelled in the Ontology-db.
LABEL_KEY_MAP_QUERY = """
MATCH (nt:NodeType)-[r:HAS_PROPERTY]->(pd:PropertyDef) WHERE r.is_key = true
RETURN nt.name AS label, pd.name AS key_prop
UNION
MATCH (tt:TextType)
RETURN tt.name AS label, 'id' AS key_prop
UNION
MATCH (dt:DocType)
RETURN dt.name AS label, 'id' AS key_prop
"""


def label_key_map_from_rows(rows) -> dict[str, str]:
    key_map = {row["label"]: row["key_prop"] for row in rows}
    key_map["Chunk"] = "id"
    return key_map


async def load_label_key_map(ontology_driver, ontology_database: str) -> dict[str, str]:
    """{label: key_prop} from the Ontology-db (with retry); see LABEL_KEY_MAP_QUERY."""
    async def _read():
        async with ontology_driver.session(database=ontology_database) as session:
            result = await session.run(LABEL_KEY_MAP_QUERY)
            return await result.data()

    return label_key_map_from_rows(await with_retry(_read))
