"""
Bluestar MCP Shared PropertyDef Type Checking

The single implementation of Ontology-db PropertyDef.type rules for every
Bluestar server, so mcp-BP-KG and mcp-kg-manager reject and convert the same
values the same way.

- coerce_and_check_property_types: incoming values (writes). Coerces in place
  (int -> float for FLOAT, strict ISO-8601 string -> date/datetime for
  DATE/DATETIME, which the Neo4j driver then stores as native temporals)
- parse_default_value: a PropertyDef.default_value string as its declared type
- stored_value_type_mismatch: values read back from Neo4j (validate_graph)
- json_default: json.dumps(default=...) for tool output, temporals as ISO
"""
import math
import re
from datetime import date, datetime, timezone

import neo4j.time

PROPERTY_TYPES = ("STRING", "BOOLEAN", "INTEGER", "FLOAT", "DATE", "DATETIME")

# The expected input form, shown in the error message for the temporal types.
INPUT_FORMS = {
    "DATE": "YYYY-MM-DD",
    "DATETIME": "YYYY-MM-DDThh:mm:ss[.ffffff] with Z or ±hh:mm",
}

# Regex gate before fromisoformat, which on Python 3.12 also accepts 20261012,
# 2026-W42-6, a space separator, offset-less datetimes and 7+ fraction digits.
# [0-9] rather than \d, which matches non-ASCII digits too; used with fullmatch.
_DATE_RE = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}")
_DATETIME_RE = re.compile(
    r"[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}(\.[0-9]{1,6})?(Z|[+-][0-9]{2}:[0-9]{2})"
)


def parse_strict_date(value) -> date | None:
    """A date from exactly YYYY-MM-DD naming a real calendar day, else None."""
    if not isinstance(value, str) or not _DATE_RE.fullmatch(value):
        return None
    try:
        return date.fromisoformat(value)
    except ValueError:
        return None


def parse_strict_datetime(value) -> datetime | None:
    """
    An aware datetime normalised to UTC from YYYY-MM-DDThh:mm:ss, an optional
    1-6 digit fraction, then a mandatory Z or ±hh:mm offset, else None.
    """
    if not isinstance(value, str) or not _DATETIME_RE.fullmatch(value):
        return None
    try:
        return datetime.fromisoformat(value).astimezone(timezone.utc)
    except ValueError:
        return None


# The acceptance rule shared by the input and stored paths: a strict string
# (input only), a Python or neo4j.time date, or an aware datetime.

def _to_date(value) -> date | None:
    """DATE: a strict string, or a value already converted (a run over the same dict, or a KG read)."""
    if isinstance(value, neo4j.time.Date):
        return value.to_native()
    if isinstance(value, date) and not isinstance(value, datetime):
        return value
    return parse_strict_date(value)


def _to_datetime(value) -> datetime | None:
    """DATETIME: a strict string, or an already converted aware value, normalised to UTC."""
    if isinstance(value, neo4j.time.DateTime):
        value = value.to_native()
    if isinstance(value, datetime):
        return value.astimezone(timezone.utc) if value.tzinfo is not None else None
    return parse_strict_datetime(value)


_CONVERTERS = {"DATE": _to_date, "DATETIME": _to_datetime}


def _basic_type_mismatch(declared_type: str, value) -> bool:
    """
    The four original types (DEF-18). bool is an int subclass in Python, so it
    is excluded from INTEGER and FLOAT explicitly. Anything unrecognised is
    never flagged: validate_ontology's type allowlist catches those upstream.
    """
    if declared_type == "STRING":
        return not isinstance(value, str)
    if declared_type == "BOOLEAN":
        return not isinstance(value, bool)
    if declared_type == "INTEGER":
        return isinstance(value, bool) or not isinstance(value, int)
    if declared_type == "FLOAT":
        return isinstance(value, bool) or not isinstance(value, float)
    return False


def _type_error(prop_name: str, declared_type: str, value) -> str:
    form = f" ({INPUT_FORMS[declared_type]})" if declared_type in INPUT_FORMS else ""
    return f"Property '{prop_name}' expects {declared_type}{form}, got {type(value).__name__} ('{value}')"


def coerce_and_check_property_types(properties: dict, prop_defs: dict) -> str | None:
    """
    Checks incoming values against their PropertyDef type and coerces them in
    place: an int on FLOAT becomes a float, and a DATE/DATETIME value becomes
    a date / an aware UTC datetime. Returns the first error message, or None.

    A None value is skipped (clearing a property is properties_to_remove's
    job), as is a property with no PropertyDef (system/audit properties; the
    unknown-property check covers the rest). Running it twice over the same
    dict gives the same result, so defaults can be merged in and re-checked.
    """
    for prop_name, value in properties.items():
        if value is None:
            continue
        prop_def = prop_defs.get(prop_name)
        if not prop_def:
            continue
        declared_type = prop_def.get("type")
        converter = _CONVERTERS.get(declared_type)
        if converter:
            converted = converter(value)
            if converted is None:
                return _type_error(prop_name, declared_type, value)
            properties[prop_name] = converted
        elif declared_type == "FLOAT" and isinstance(value, int) and not isinstance(value, bool):
            properties[prop_name] = float(value)
        elif _basic_type_mismatch(declared_type, value):
            return _type_error(prop_name, declared_type, value)
    return None


_INTEGER_TEXT_RE = re.compile(r"[+-]?[0-9]+")
_FLOAT_TEXT_RE = re.compile(r"[+-]?([0-9]+(\.[0-9]*)?|\.[0-9]+)([eE][+-]?[0-9]+)?")


def _parse_float_text(text: str) -> float | None:
    if not _FLOAT_TEXT_RE.fullmatch(text):
        return None
    value = float(text)
    return value if math.isfinite(value) else None  # "1e999" overflows to inf


_TEXT_PARSERS = {
    "INTEGER": lambda text: int(text) if _INTEGER_TEXT_RE.fullmatch(text) else None,
    "FLOAT": _parse_float_text,
    "BOOLEAN": lambda text: {"true": True, "false": False}.get(text),
    "DATE": parse_strict_date,
    "DATETIME": parse_strict_datetime,
}


def parse_default_value(declared_type: str, text, prop_name: str = "default_value") -> tuple[object, str | None]:
    """
    A PropertyDef.default_value (stored as a string in Ontology-db) as a value
    of its declared type: returns (value, None), or (None, error) in the same
    format as coerce_and_check_property_types.

    Strict: INTEGER from an optionally signed run of digits ("5"), FLOAT from
    a plain decimal ("0.0", "5", "1e3"; never "nan"/"inf"), BOOLEAN from
    exactly "true"/"false", DATE/DATETIME through the strict ISO parsers, and
    STRING unchanged. A default that isn't a string goes through the normal
    input check, so one already of the right type is accepted. Unrecognised
    types are returned unchanged, as elsewhere.
    """
    if not isinstance(text, str):
        properties = {prop_name: text}
        error = coerce_and_check_property_types(properties, {prop_name: {"type": declared_type}})
        return (None, error) if error else (properties[prop_name], None)
    parser = _TEXT_PARSERS.get(declared_type)
    if parser is None:
        return text, None
    value = parser(text)
    if value is None:
        return None, _type_error(prop_name, declared_type, text)
    return value, None


def stored_value_type_mismatch(declared_type: str, value) -> bool:
    """
    Whether a value read from Neo4j doesn't match its declared type (for
    validate_graph). DATE/DATETIME use the same acceptance rule as the input
    path, minus strings: a date (Python or neo4j.time, not a datetime) on
    DATE, an aware datetime on DATETIME. A string is a mismatch, which is how
    legacy string dates show up; so is a naive datetime or a LocalDateTime.
    An int on FLOAT is a mismatch too: Neo4j stores int and float as
    distinct types.
    """
    converter = _CONVERTERS.get(declared_type)
    if converter:
        return isinstance(value, str) or converter(value) is None
    return _basic_type_mismatch(declared_type, value)


def _utc_iso(value: datetime) -> str:
    utc = value.astimezone(timezone.utc)
    fraction = f".{utc.microsecond:06d}" if utc.microsecond else ""
    return utc.strftime("%Y-%m-%dT%H:%M:%S") + fraction + "Z"


def json_default(value):
    """
    For json.dumps(..., default=json_default). Dates become YYYY-MM-DD and
    aware datetimes YYYY-MM-DDThh:mm:ss[.ffffff]Z in UTC (Neo4j nanoseconds
    are truncated to microseconds). Anything else becomes str(value), as with
    default=str.
    """
    if isinstance(value, neo4j.time.DateTime) and value.tzinfo is not None:
        return _utc_iso(value.to_native())
    if isinstance(value, neo4j.time.Date):
        return value.iso_format()
    if isinstance(value, datetime) and value.tzinfo is not None:
        return _utc_iso(value)
    if isinstance(value, date) and not isinstance(value, datetime):
        return value.isoformat()
    return str(value)
