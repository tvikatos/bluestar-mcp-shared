import json
from datetime import date, datetime, timedelta, timezone

import neo4j.time
import pytest

from bluestar_mcp_shared.property_types import (
    PROPERTY_TYPES,
    coerce_and_check_property_types,
    json_default,
    parse_strict_date,
    parse_strict_datetime,
    stored_value_type_mismatch,
)

UTC = timezone.utc


def check(declared_type, value):
    """Runs one property through coerce_and_check_property_types; returns (error, stored value)."""
    properties = {"p": value}
    error = coerce_and_check_property_types(properties, {"p": {"type": declared_type}})
    return error, properties["p"]


def test_property_types():
    assert PROPERTY_TYPES == ("STRING", "BOOLEAN", "INTEGER", "FLOAT", "DATE", "DATETIME")


# ---- parse_strict_date / parse_strict_datetime ----

def test_parse_strict_date_accepts_iso_date():
    assert parse_strict_date("2026-10-12") == date(2026, 10, 12)
    assert parse_strict_date("2024-02-29") == date(2024, 2, 29)


@pytest.mark.parametrize("value", [
    "2026-2-1", "20261012", "2026-W42-6", "2026-02-30", "2025-02-29", "2026-13-01",
    "12/10/26", "next quarter", "2026-10-12T00:00:00Z", " 2026-10-12", "2026-10-12\n",
    "２０２６-10-12", "", True, 20261012, None, date(2026, 10, 12),
])
def test_parse_strict_date_rejects(value):
    assert parse_strict_date(value) is None


@pytest.mark.parametrize("value, expected", [
    ("2026-10-12T10:00:00Z", datetime(2026, 10, 12, 10, 0, tzinfo=UTC)),
    ("2026-10-12T10:00:00+01:00", datetime(2026, 10, 12, 9, 0, tzinfo=UTC)),
    ("2026-10-12T10:00:00-05:30", datetime(2026, 10, 12, 15, 30, tzinfo=UTC)),
    ("2026-10-12T10:00:00.123Z", datetime(2026, 10, 12, 10, 0, 0, 123000, tzinfo=UTC)),
    ("2026-10-12T10:00:00.1+00:00", datetime(2026, 10, 12, 10, 0, 0, 100000, tzinfo=UTC)),
    ("2026-10-12T10:00:00.123456Z", datetime(2026, 10, 12, 10, 0, 0, 123456, tzinfo=UTC)),
    ("2026-10-12T00:30:00+01:00", datetime(2026, 10, 11, 23, 30, tzinfo=UTC)),
])
def test_parse_strict_datetime_accepts_and_normalises_to_utc(value, expected):
    parsed = parse_strict_datetime(value)
    assert parsed == expected
    assert parsed.utcoffset() == timedelta(0)


@pytest.mark.parametrize("value", [
    "2026-10-12T10:00:00",            # no offset
    "2026-10-12 10:00:00Z",           # space separator
    "2026-10-12T10:00:00.1234567Z",   # 7 fraction digits
    "2026-10-12T10:00Z",              # no seconds
    "2026-10-12T10:00:00+0100",       # offset without colon
    "2026-10-12T10:00:00+01",         # offset without minutes
    "2026-10-12t10:00:00z",           # lowercase
    "2026-10-12T24:00:00Z", "2026-10-12T10:00:60Z", "2026-02-30T10:00:00Z",
    "2026-10-12T10:00:00+25:00", "2026-10-12", "20261012T100000Z", "",
    True, 1760263200, None, datetime(2026, 10, 12, 10, tzinfo=UTC),
])
def test_parse_strict_datetime_rejects(value):
    assert parse_strict_datetime(value) is None


# ---- coerce_and_check_property_types: the four original types ----

@pytest.mark.parametrize("declared_type, value", [
    ("STRING", "x"), ("BOOLEAN", True), ("BOOLEAN", False), ("INTEGER", 3), ("INTEGER", -1),
    ("FLOAT", 1.5),
])
def test_basic_types_accept(declared_type, value):
    assert check(declared_type, value) == (None, value)


@pytest.mark.parametrize("declared_type, value", [
    ("STRING", 3), ("STRING", True), ("BOOLEAN", "true"), ("BOOLEAN", 1), ("INTEGER", True),
    ("INTEGER", 1.0), ("INTEGER", "3"), ("FLOAT", True), ("FLOAT", "1.5"),
])
def test_basic_types_reject(declared_type, value):
    error, stored = check(declared_type, value)
    assert error == f"Property 'p' expects {declared_type}, got {type(value).__name__} ('{value}')"
    assert stored == value


def test_int_on_float_is_coerced_in_place():
    error, stored = check("FLOAT", 2)
    assert error is None
    assert stored == 2.0 and isinstance(stored, float)


def test_none_and_undeclared_and_unknown_types_are_skipped():
    properties = {"cleared": None, "audit": object(), "odd": "anything", "n": 1}
    prop_defs = {"cleared": {"type": "DATE"}, "odd": {"type": "STRNG"}, "n": {"type": "INTEGER"}}
    assert coerce_and_check_property_types(properties, prop_defs) is None
    assert properties["cleared"] is None and properties["odd"] == "anything"


def test_missing_type_is_never_rejected():
    assert coerce_and_check_property_types({"p": 1}, {"p": {}}) is None


def test_returns_first_error():
    properties = {"a": 1, "b": "x", "c": "y"}
    prop_defs = {"a": {"type": "INTEGER"}, "b": {"type": "INTEGER"}, "c": {"type": "BOOLEAN"}}
    assert coerce_and_check_property_types(properties, prop_defs) == (
        "Property 'b' expects INTEGER, got str ('x')"
    )


# ---- coerce_and_check_property_types: DATE / DATETIME ----

def test_date_string_is_converted_in_place():
    error, stored = check("DATE", "2026-10-12")
    assert error is None
    assert stored == date(2026, 10, 12) and type(stored) is date


def test_datetime_string_is_converted_to_utc_in_place():
    error, stored = check("DATETIME", "2026-10-12T10:00:00+01:00")
    assert error is None
    assert stored == datetime(2026, 10, 12, 9, 0, tzinfo=UTC)
    assert stored.tzinfo == UTC


@pytest.mark.parametrize("value", [
    "2026-2-1", "20261012", "2026-W42-6", "2026-02-30", "12/10/26",
    datetime(2026, 10, 12, tzinfo=UTC), neo4j.time.DateTime(2026, 10, 12, tzinfo=UTC),
    True, 20261012, 1.5,
])
def test_date_rejects(value):
    error, stored = check("DATE", value)
    assert error == f"Property 'p' expects DATE (YYYY-MM-DD), got {type(value).__name__} ('{value}')"
    assert stored is value


@pytest.mark.parametrize("value", [
    "2026-10-12T10:00:00", "2026-10-12 10:00:00Z", "2026-10-12T10:00:00.1234567Z",
    "2026-10-12", datetime(2026, 10, 12, 10), neo4j.time.DateTime(2026, 10, 12, 10),
    date(2026, 10, 12), True, 1760263200, 1.5,
])
def test_datetime_rejects(value):
    error, stored = check("DATETIME", value)
    assert error == (
        "Property 'p' expects DATETIME (YYYY-MM-DDThh:mm:ss[.ffffff] with Z or ±hh:mm), "
        f"got {type(value).__name__} ('{value}')"
    )
    assert stored is value


def test_error_message_format():
    error, _ = check("DATE", "12/10/26")
    assert error == "Property 'p' expects DATE (YYYY-MM-DD), got str ('12/10/26')"
    properties = {"fromDate": "12/10/26"}
    assert coerce_and_check_property_types(properties, {"fromDate": {"type": "DATE"}}) == (
        "Property 'fromDate' expects DATE (YYYY-MM-DD), got str ('12/10/26')"
    )


def test_already_converted_values_are_accepted():
    assert check("DATE", date(2026, 10, 12)) == (None, date(2026, 10, 12))
    error, stored = check("DATETIME", datetime(2026, 10, 12, 10, tzinfo=timezone(timedelta(hours=2))))
    assert error is None
    assert stored == datetime(2026, 10, 12, 8, tzinfo=UTC) and stored.tzinfo == UTC


def test_values_read_from_the_kg_validate_when_resent():
    error, stored = check("DATE", neo4j.time.Date(2026, 10, 12))
    assert error is None
    assert stored == date(2026, 10, 12) and type(stored) is date
    error, stored = check("DATETIME", neo4j.time.DateTime(2026, 10, 12, 10, 0, 0, 123456789, tzinfo=UTC))
    assert error is None
    assert stored == datetime(2026, 10, 12, 10, 0, 0, 123456, tzinfo=UTC)
    assert type(stored) is datetime


def test_running_twice_gives_the_same_result():
    prop_defs = {
        "name": {"type": "STRING"}, "count": {"type": "INTEGER"}, "ratio": {"type": "FLOAT"},
        "fromDate": {"type": "DATE"}, "at": {"type": "DATETIME"}, "done": {"type": "BOOLEAN"},
    }
    properties = {
        "name": "x", "count": 3, "ratio": 2, "fromDate": "2026-10-12",
        "at": "2026-10-12T10:00:00.5-05:30", "done": False,
    }
    assert coerce_and_check_property_types(properties, prop_defs) is None
    first = dict(properties)
    assert coerce_and_check_property_types(properties, prop_defs) is None
    assert properties == first
    assert {k: type(v) for k, v in properties.items()} == {k: type(v) for k, v in first.items()}
    assert first["ratio"] == 2.0 and first["fromDate"] == date(2026, 10, 12)
    assert first["at"] == datetime(2026, 10, 12, 15, 30, 0, 500000, tzinfo=UTC)


# ---- stored_value_type_mismatch ----

@pytest.mark.parametrize("declared_type, value", [
    ("DATE", neo4j.time.Date(2026, 10, 12)),
    ("DATE", date(2026, 10, 12)),
    ("DATETIME", neo4j.time.DateTime(2026, 10, 12, 10, tzinfo=UTC)),
    ("DATETIME", datetime(2026, 10, 12, 10, tzinfo=timezone(timedelta(hours=2)))),
    ("STRING", "x"), ("BOOLEAN", True), ("INTEGER", 3), ("FLOAT", 1.5),
    ("UNKNOWN", object()),
])
def test_stored_value_matches(declared_type, value):
    assert stored_value_type_mismatch(declared_type, value) is False


@pytest.mark.parametrize("declared_type, value", [
    ("DATE", "2026-10-12"),                                  # legacy string date
    ("DATE", neo4j.time.DateTime(2026, 10, 12, tzinfo=UTC)),
    ("DATE", datetime(2026, 10, 12, tzinfo=UTC)),
    ("DATE", True), ("DATE", 20261012),
    ("DATETIME", "2026-10-12T10:00:00Z"),                    # legacy string datetime
    ("DATETIME", neo4j.time.DateTime(2026, 10, 12, 10)),     # LocalDateTime: no timezone
    ("DATETIME", datetime(2026, 10, 12, 10)),                # naive
    ("DATETIME", neo4j.time.Date(2026, 10, 12)),
    ("DATETIME", date(2026, 10, 12)),
    ("FLOAT", 3),                                            # int on FLOAT
    ("INTEGER", True), ("STRING", 3),
])
def test_stored_value_mismatches(declared_type, value):
    assert stored_value_type_mismatch(declared_type, value) is True


# ---- json_default ----

@pytest.mark.parametrize("value, expected", [
    (neo4j.time.Date(2026, 10, 12), "2026-10-12"),
    (date(2026, 10, 12), "2026-10-12"),
    (neo4j.time.DateTime(2026, 10, 12, 10, 0, 0, tzinfo=UTC), "2026-10-12T10:00:00Z"),
    (neo4j.time.DateTime(2026, 10, 12, 10, 0, 0, 123456789, tzinfo=UTC), "2026-10-12T10:00:00.123456Z"),
    (neo4j.time.DateTime(2026, 10, 12, 10, 0, 0, tzinfo=timezone(timedelta(hours=1))), "2026-10-12T09:00:00Z"),
    (datetime(2026, 10, 12, 10, 0, 0, 500000, tzinfo=timezone(timedelta(hours=-5))), "2026-10-12T15:00:00.500000Z"),
])
def test_json_default_temporals(value, expected):
    assert json_default(value) == expected


def test_json_default_falls_back_to_str():
    local = neo4j.time.DateTime(2026, 10, 12, 10)
    naive = datetime(2026, 10, 12, 10)
    assert json_default(local) == str(local)
    assert json_default(naive) == str(naive)
    assert json_default({1, 2}) == str({1, 2})


def test_json_default_with_json_dumps():
    row = {"fromDate": neo4j.time.Date(2026, 1, 2), "at": neo4j.time.DateTime(2026, 1, 2, 3, 4, 5, tzinfo=UTC)}
    assert json.dumps(row, default=json_default) == '{"fromDate": "2026-01-02", "at": "2026-01-02T03:04:05Z"}'
