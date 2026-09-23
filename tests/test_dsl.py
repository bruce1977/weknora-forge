"""FMQ syntax and evaluation tests."""

from __future__ import annotations

import pytest

from app.services.meta_dsl import (
    build_fields,
    compile_sql,
    describe,
    evaluate,
    parse_query,
    used_fields,
)


def q(expr: str, metas: dict, builtins: dict | None = None, ci: bool = False) -> bool:
    node = parse_query(expr)
    record = {"id": "k1", "title": "T", "parse_status": "completed", **(builtins or {})}
    return evaluate(node, build_fields(record, metas), ci)


@pytest.mark.parametrize(
    "expr,metas,expected",
    [
        ("level = 3", {"level": 3}, True),
        ("level == 3", {"level": "3"}, True),
        ("level >= 3", {"level": 4}, True),
        ("level >= 3", {"level": 2}, False),
        ("level < 3", {"level": 2.5}, True),
        ("level != 3", {"level": 5}, True),
        ("category = 'tech'", {"category": "tech"}, True),
        ("category = tech", {"category": "tech"}, True),
        ("category != 'tech'", {"category": "tech"}, False),
        ("tags CONTAINS 'ai'", {"tags": ["ai", "db"]}, True),
        ("tags CONTAINS 'ai'", {"tags": "ai-model"}, True),
        ("tags NOT CONTAINS 'ai'", {"tags": ["db"]}, True),
        ("category IN ('tech', 'news')", {"category": "news"}, True),
        ("category NOT IN ('tech', 'news')", {"category": "news"}, False),
        ("author EXISTS", {"author": "bruce"}, True),
        ("author EXISTS", {}, False),
        ("NOT author EXISTS", {}, True),
        ("draft NOT EXISTS", {}, True),
        ("title STARTSWITH 'We'", {"title": "WeKnora"}, True),
        ("title ENDSWITH 'ora'", {"title": "WeKnora"}, True),
        ("code MATCHES '^A-\\d+$'", {"code": "A-123"}, True),
        ("code MATCHES '^A-\\d+$'", {"code": "B-123"}, False),
        (
            "published_at >= '2026-01-01'",
            {"published_at": "2026-03-05T10:00:00+08:00"},
            True,
        ),
        ("published_at < '2026-01-01'", {"published_at": "2025-12-31 23:00:00"}, True),
        ("nested.a = 1", {"nested": {"a": 1}}, True),
    ],
)
def test_single_conditions(expr, metas, expected):
    assert q(expr, metas) is expected


@pytest.mark.parametrize(
    "expr,metas,expected",
    [
        ("level >= 3 AND category = 'tech'", {"level": 3, "category": "tech"}, True),
        ("level >= 3 AND category = 'tech'", {"level": 1, "category": "tech"}, False),
        ("level >= 3 OR category = 'tech'", {"level": 1, "category": "tech"}, True),
        ("(a = 1 OR b = 2) AND c = 3", {"a": 1, "c": 3}, True),
        ("(a = 1 OR b = 2) AND c = 3", {"b": 2, "c": 4}, False),
        ("NOT (a = 1 AND b = 2)", {"a": 1, "b": 3}, True),
        ("$parse_status = 'completed' AND level > 1", {"level": 2}, True),
        ("$parse_status = 'failed' AND level > 1", {"level": 2}, False),
        ("level > 1 && level < 5", {"level": 3}, True),
        ("level = 1 || level = 9", {"level": 9}, True),
    ],
)
def test_boolean_combinations(expr, metas, expected):
    assert q(expr, metas) is expected


def test_builtin_field_override():
    node = parse_query("$title = 'WeKnora'")
    assert evaluate(node, build_fields({"title": "WeKnora"}, {}))
    assert not evaluate(node, build_fields({"title": "other"}, {}))


def test_case_insensitive_option():
    node = parse_query("category = 'Tech'")
    assert not evaluate(node, build_fields({}, {"category": "tech"}))
    assert evaluate(node, build_fields({}, {"category": "tech"}), True)


def test_missing_field_semantics():
    assert q("ghost != 'x'", {}) is True
    assert q("ghost = 'x'", {}) is False
    assert q("ghost > 3", {}) is False


def test_ast_and_fields():
    node = parse_query("level >= 3 AND tags CONTAINS 'ai'")
    assert used_fields(node) == ["level", "tags"]
    ast = describe(node)
    assert ast["type"] == "and" and len(ast["items"]) == 2


def test_sql_pushdown_postgres():
    """compiled SQL targets custom_metadata::jsonb with native ->> / -> operators"""
    node = parse_query("level >= 3 AND tags CONTAINS 'ai' AND code MATCHES '^A'")
    sql, params = compile_sql(
        node,
        metas_column="custom_metadata",
        builtin_columns={"parse_status": "parse_status"},
    )
    assert "custom_metadata" in sql
    assert "->> 'level'" in sql  # text form (ordering fallback)
    assert "-> 'level'" in sql  # jsonb form (native ordering)
    assert "AS jsonb" in sql  # jsonb literal comparisons (value bound as text)
    assert "LIKE" in sql  # CONTAINS
    assert "1=1" in sql  # regex is not pushed down
    # jsonb literals travel as JSON text and are cast with CAST(... AS jsonb) in SQL
    assert set(params.values()) == {"3", "%ai%"}


def test_sql_pushdown_builtin_column():
    node = parse_query("$parse_status = 'completed'")
    sql, params = compile_sql(node, builtin_columns={"parse_status": "k.parse_status"})
    assert "k.parse_status" in sql
    assert "completed" in params.values()


def test_sql_pushdown_not_is_superset():
    """Negating a widened predicate would drop rows, so NOT falls back to 1=1."""
    node = parse_query("NOT (level = 3)")
    sql, _ = compile_sql(node)
    assert sql.strip() == "1=1"

    node = parse_query("NOT author EXISTS")
    sql, _ = compile_sql(node)
    assert "= ''" in sql  # NOT EXISTS stays exact, therefore it is pushed down


def test_sql_pushdown_exists_and_jsonb_null():
    node = parse_query("author EXISTS")
    sql, _ = compile_sql(node)
    assert "COALESCE" in sql and "<> ''" in sql


def test_syntax_errors():
    from app.errors import ForgeError

    for bad in ["", "level =", "= 3", "level 3", "((a = 1)"]:
        with pytest.raises(ForgeError):
            parse_query(bad)
