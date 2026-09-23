"""Custom Metas search syntax (Forge Meta Query Language, FMQ for short).

Goal: a single expression that is human readable, URL friendly and can be pushed
down to SQL, covering "equal / greater than / lower than / contains" style lookups
over one or more metas.

Grammar
-------
    expr        := or
    or          := and (("OR" | "||") and)*
    and         := not (("AND" | "&&") not)*
    not         := ("NOT" | "!") not | primary
    primary     := "(" expr ")" | comparison
    comparison  := field op operand | field "EXISTS" | field "NOT" "EXISTS"
    operand     := literal | "(" literal ("," literal)* ")"

Supported operators
-------------------
    = ==            equal (numbers / dates / strings are compared using the best matching type)
    != <>           not equal
    > >= < <=       greater / greater or equal / lower / lower or equal
    CONTAINS        substring match; membership test for lists / dicts
    NOT CONTAINS    negated CONTAINS
    IN / NOT IN     contained in / not contained in the right-hand list
    EXISTS / NOT EXISTS  the field exists and is not empty
    STARTSWITH / ENDSWITH  prefix / suffix match
    MATCHES ~       regex match (Python re, case sensitive)

Literals
--------
    'string' or "string"   numbers 123 / 3.14   true / false   null

Fields
------
    category        write the meta key directly (a.b nested paths are supported)
    "author name"   wrap in quotes when it contains special characters
    $title          built-in fields always start with $:
                    $id $title $kb_id $tag_id $type $file_type $source
                    $parse_status $enable_status $created_at $updated_at

Examples
--------
    level >= 3 AND category = 'tech'
    tags CONTAINS 'ai' AND (author = 'bruce' OR author = 'ada')
    published_at >= '2026-01-01' AND NOT draft EXISTS
    $parse_status = 'completed' AND source IN ('web', 'api')
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Dict, List, Optional, Tuple

from ..errors import bad_request

# --------------------------------------------------------------------------- #
# AST
# --------------------------------------------------------------------------- #


class Op(str, Enum):
    EQ = "="
    NE = "!="
    GT = ">"
    GTE = ">="
    LT = "<"
    LTE = "<="
    CONTAINS = "contains"
    NOT_CONTAINS = "not_contains"
    IN = "in"
    NOT_IN = "not_in"
    EXISTS = "exists"
    NOT_EXISTS = "not_exists"
    STARTSWITH = "startswith"
    ENDSWITH = "endswith"
    MATCHES = "matches"


OP_ALIASES: Dict[str, Op] = {
    "=": Op.EQ,
    "==": Op.EQ,
    "!=": Op.NE,
    "<>": Op.NE,
    ">": Op.GT,
    ">=": Op.GTE,
    "<": Op.LT,
    "<=": Op.LTE,
    "~": Op.MATCHES,
    "contains": Op.CONTAINS,
    "in": Op.IN,
    "exists": Op.EXISTS,
    "startswith": Op.STARTSWITH,
    "endswith": Op.ENDSWITH,
    "matches": Op.MATCHES,
    "like": Op.CONTAINS,
}

BUILTIN_FIELDS = {
    "id",
    "title",
    "kb_id",
    "tag_id",
    "type",
    "file_type",
    "source",
    "parse_status",
    "enable_status",
    "created_at",
    "updated_at",
    "deleted_at",
    "description",
}


@dataclass
class Cmp:
    field: str
    op: Op
    values: List[Any] = field(default_factory=list)


@dataclass
class Bool:
    op: str  # and | or
    items: List["Node"] = field(default_factory=list)


@dataclass
class Not:
    item: "Node"


Node = Cmp or Bool or Not  # readability only


# --------------------------------------------------------------------------- #
# Lexer
# --------------------------------------------------------------------------- #

_TOKEN_RE = re.compile(
    r"""
    \s+
    |(?P<lparen>\()
    |(?P<rparen>\))
    |(?P<comma>,)
    |(?P<op>&&|\|\||<>|>=|<=|!=|==|=|>|<|~)
    |(?P<string>'[^']*'|"[^"]*")
    |(?P<number>-?\d+(?:\.\d+)?)
    |(?P<ident>[A-Za-z_$][A-Za-z0-9_.\-$]*)
    |(?P<other>.)
    """,
    re.VERBOSE,
)

_KEYWORDS = {
    "and": "AND",
    "or": "OR",
    "not": "NOT",
    "true": "TRUE",
    "false": "FALSE",
    "null": "NULL",
    "contains": "WORD",
    "in": "WORD",
    "exists": "WORD",
    "startswith": "WORD",
    "endswith": "WORD",
    "matches": "WORD",
    "like": "WORD",
}


@dataclass
class Token:
    kind: str  # LPAREN RPAREN COMMA OP STRING NUMBER IDENT AND OR NOT TRUE FALSE NULL WORD EOF
    value: str
    pos: int = 0


def tokenize(text: str) -> List[Token]:
    tokens: List[Token] = []
    pos = 0
    for m in _TOKEN_RE.finditer(text):
        pos = m.start()
        if m.lastgroup is None:
            continue
        kind, value = m.lastgroup, m.group()
        if kind == "lparen":
            tokens.append(Token("LPAREN", "(", pos))
        elif kind == "rparen":
            tokens.append(Token("RPAREN", ")", pos))
        elif kind == "comma":
            tokens.append(Token("COMMA", ",", pos))
        elif kind == "op":
            if value == "&&":
                tokens.append(Token("AND", value, pos))
            elif value == "||":
                tokens.append(Token("OR", value, pos))
            else:
                tokens.append(Token("OP", value, pos))
        elif kind == "string":
            tokens.append(Token("STRING", value[1:-1], pos))
        elif kind == "number":
            tokens.append(Token("NUMBER", value, pos))
        elif kind == "ident":
            lowered = value.lower()
            mapped = _KEYWORDS.get(lowered)
            if mapped == "AND":
                tokens.append(Token("AND", value, pos))
            elif mapped == "OR":
                tokens.append(Token("OR", value, pos))
            elif mapped == "NOT":
                tokens.append(Token("NOT", value, pos))
            elif mapped == "TRUE":
                tokens.append(Token("TRUE", "true", pos))
            elif mapped == "FALSE":
                tokens.append(Token("FALSE", "false", pos))
            elif mapped == "NULL":
                tokens.append(Token("NULL", "null", pos))
            elif mapped == "WORD":
                tokens.append(Token("WORD", lowered, pos))
            else:
                tokens.append(Token("IDENT", value, pos))
        else:
            raise bad_request(
                f"Unrecognized character '{value}' (position {pos})",
                details={"position": pos},
            )
    tokens.append(Token("EOF", "", len(text)))
    return tokens


# --------------------------------------------------------------------------- #
# Parser
# --------------------------------------------------------------------------- #


class Parser:
    def __init__(self, tokens: List[Token]) -> None:
        self.tokens = tokens
        self.i = 0

    @property
    def cur(self) -> Token:
        return self.tokens[self.i]

    def next(self) -> Token:
        tok = self.tokens[self.i]
        self.i += 1
        return tok

    def expect(self, kind: str) -> Token:
        tok = self.cur
        if tok.kind != kind:
            raise bad_request(
                f"Syntax error: expected {kind}, got '{tok.value or 'EOF'}' (position {tok.pos})"
            )
        return self.next()

    def parse(self) -> Any:
        node = self.parse_or()
        if self.cur.kind != "EOF":
            raise bad_request(
                f"Syntax error: unexpected trailing content '{self.cur.value}' (position {self.cur.pos})"
            )
        return node

    def parse_or(self) -> Any:
        items = [self.parse_and()]
        while self.cur.kind == "OR":
            self.next()
            items.append(self.parse_and())
        return items[0] if len(items) == 1 else Bool("or", items)

    def parse_and(self) -> Any:
        items = [self.parse_not()]
        while self.cur.kind == "AND":
            self.next()
            items.append(self.parse_not())
        return items[0] if len(items) == 1 else Bool("and", items)

    def parse_not(self) -> Any:
        if self.cur.kind == "NOT":
            self.next()
            return Not(self.parse_not())
        return self.parse_primary()

    def parse_primary(self) -> Any:
        if self.cur.kind == "LPAREN":
            # Either a grouped expression or an operand list (only appears inside a comparison)
            save = self.i
            self.next()
            if self._looks_like_operand_list():
                self.i = save
                raise bad_request(
                    "Syntax error: a comparison expression is expected here"
                )
            node = self.parse_or()
            self.expect("RPAREN")
            return node
        return self.parse_comparison()

    def _looks_like_operand_list(self) -> bool:
        return self.cur.kind in {"STRING", "NUMBER", "TRUE", "FALSE", "NULL"}

    def parse_field(self) -> str:
        tok = self.cur
        if tok.kind == "IDENT":
            self.next()
            return tok.value
        if tok.kind == "STRING":
            self.next()
            return tok.value
        raise bad_request(
            f"Syntax error: expected a field name, got '{tok.value or 'EOF'}' (position {tok.pos})"
        )

    def parse_comparison(self) -> Cmp:
        name = self.parse_field()

        # NOT CONTAINS / NOT IN / NOT EXISTS
        negated = False
        if self.cur.kind == "NOT":
            self.next()
            negated = True

        op: Optional[Op] = None
        if self.cur.kind == "OP":
            op = OP_ALIASES.get(self.next().value.lower())
        elif self.cur.kind == "WORD":
            word = self.next().value.lower()
            op = OP_ALIASES.get(word)
        elif self.cur.kind == "TRUE" or self.cur.kind == "FALSE":
            # e.g. `draft = true`
            op = Op.EQ

        if op is None:
            if self.cur.kind == "EOF" or self.cur.kind == "RPAREN":
                op = Op.EXISTS  # a bare field means an existence check
            else:
                raise bad_request(
                    f"Syntax error: missing operator after field '{name}' (position {self.cur.pos})",
                    details={
                        "hint": "Available: = != > >= < <= CONTAINS IN EXISTS STARTSWITH ENDSWITH MATCHES"
                    },
                )

        if negated:
            op = {
                Op.CONTAINS: Op.NOT_CONTAINS,
                Op.IN: Op.NOT_IN,
                Op.EXISTS: Op.NOT_EXISTS,
                Op.EQ: Op.NE,
            }.get(op, op)

        if op in (Op.EXISTS, Op.NOT_EXISTS):
            return Cmp(name, op, [])

        values = self.parse_operands()
        if not values:
            raise bad_request(
                f"Syntax error: operator {op.value} is missing an operand (field {name})"
            )
        if (
            op
            in (
                Op.EQ,
                Op.NE,
                Op.GT,
                Op.GTE,
                Op.LT,
                Op.LTE,
                Op.MATCHES,
                Op.STARTSWITH,
                Op.ENDSWITH,
            )
            and len(values) > 1
        ):
            raise bad_request(
                f"Syntax error: operator {op.value} accepts a single operand only (field {name})"
            )
        return Cmp(name, op, values)

    def parse_operands(self) -> List[Any]:
        if self.cur.kind == "LPAREN":
            self.next()
            values = [self.parse_literal()]
            while self.cur.kind == "COMMA":
                self.next()
                values.append(self.parse_literal())
            self.expect("RPAREN")
            return values
        return [self.parse_literal()]

    def parse_literal(self) -> Any:
        tok = self.next()
        if tok.kind == "STRING":
            return tok.value
        if tok.kind == "NUMBER":
            return float(tok.value) if "." in tok.value else int(tok.value)
        if tok.kind == "TRUE":
            return True
        if tok.kind == "FALSE":
            return False
        if tok.kind == "NULL":
            return None
        if tok.kind == "IDENT":
            # A bare word is accepted as a string literal (e.g. category = tech)
            return tok.value
        raise bad_request(
            f"Syntax error: expected a literal, got '{tok.value or 'EOF'}' (position {tok.pos})"
        )


def parse_query(text: str) -> Any:
    if not text or not text.strip():
        raise bad_request("Query expression must not be empty")
    return Parser(tokenize(text)).parse()


# --------------------------------------------------------------------------- #
# Value lookup and comparison
# --------------------------------------------------------------------------- #


def resolve_field(fields: Dict[str, Any], name: str) -> Tuple[bool, Any]:
    """Look up a value by name, supporting a.b nested paths; returns (exists, value)."""
    if name in fields:
        return True, fields[name]
    if "." in name:
        cur: Any = fields
        for part in name.split("."):
            if isinstance(cur, dict) and part in cur:
                cur = cur[part]
            else:
                return False, None
        return True, cur
    return False, None


def _to_number(value: Any) -> Optional[float]:
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str):
        try:
            return float(value.strip())
        except ValueError:
            return None
    return None


def _to_datetime(value: Any) -> Optional[datetime]:
    if isinstance(value, datetime):
        return value
    if isinstance(value, (int, float)):
        return datetime.fromtimestamp(value)
    if isinstance(value, str):
        text = value.strip()
        if not text:
            return None
        normalized = text.replace("Z", "+00:00")
        try:
            return datetime.fromisoformat(normalized)
        except ValueError:
            for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d", "%Y/%m/%d", "%Y年%m月%d日"):
                try:
                    return datetime.strptime(text, fmt)
                except ValueError:
                    continue
    return None


def _align_tz(a: datetime, b: datetime) -> Tuple[datetime, datetime]:
    """Normalize mixed aware/naive datetimes to naive UTC so comparisons never raise TypeError."""
    if a.tzinfo is not None and b.tzinfo is not None:
        return a, b
    if a.tzinfo is not None:
        a = a.astimezone(timezone.utc).replace(tzinfo=None)
    if b.tzinfo is not None:
        b = b.astimezone(timezone.utc).replace(tzinfo=None)
    return a, b


def _compare_scalar(actual: Any, expected: Any, op: Op, case_insensitive: bool) -> bool:
    if actual is None or expected is None:
        if op == Op.EQ:
            return actual is None and expected is None
        if op == Op.NE:
            return not (actual is None and expected is None)
        return False

    # Numbers first
    a_num, e_num = _to_number(actual), _to_number(expected)
    if a_num is not None and e_num is not None:
        return _apply(op, a_num, e_num)

    # Then dates (mixed tz-aware and naive values are normalized to UTC before comparing)
    if isinstance(expected, str) or isinstance(actual, (str, datetime)):
        a_dt, e_dt = _to_datetime(actual), _to_datetime(expected)
        if a_dt is not None and e_dt is not None:
            a_dt, e_dt = _align_tz(a_dt, e_dt)
            return _apply(op, a_dt, e_dt)

    a_str, e_str = str(actual), str(expected)
    if case_insensitive:
        a_str, e_str = a_str.lower(), e_str.lower()

    if op in (Op.GT, Op.GTE, Op.LT, Op.LTE):
        return _apply(op, a_str, e_str)
    if op == Op.EQ:
        return a_str == e_str
    if op == Op.NE:
        return a_str != e_str
    if op == Op.STARTSWITH:
        return a_str.startswith(e_str)
    if op == Op.ENDSWITH:
        return a_str.endswith(e_str)
    if op == Op.MATCHES:
        try:
            return re.search(e_str, a_str) is not None
        except re.error as exc:
            raise bad_request(f"Invalid regular expression '{e_str}': {exc}") from exc
    if op == Op.CONTAINS:
        return e_str in a_str
    if op == Op.NOT_CONTAINS:
        return e_str not in a_str
    return False


def _apply(op: Op, a: Any, b: Any) -> bool:
    if op == Op.EQ:
        return a == b
    if op == Op.NE:
        return a != b
    if op == Op.GT:
        return a > b
    if op == Op.GTE:
        return a >= b
    if op == Op.LT:
        return a < b
    if op == Op.LTE:
        return a <= b
    return False


def _is_empty(value: Any) -> bool:
    return value is None or value == "" or value == [] or value == {}


def eval_cmp(cmp: Cmp, fields: Dict[str, Any], case_insensitive: bool) -> bool:
    if cmp.op in (Op.EXISTS, Op.NOT_EXISTS):
        found, value = resolve_field(fields, cmp.field)
        truthy = found and not _is_empty(value)
        return truthy if cmp.op == Op.EXISTS else not truthy

    found, actual = resolve_field(fields, cmp.field)

    if cmp.op in (Op.IN, Op.NOT_IN):
        expected_list = (
            cmp.values[0]
            if len(cmp.values) == 1 and isinstance(cmp.values[0], (list, tuple))
            else cmp.values
        )
        hit = any(
            _compare_scalar(actual, v, Op.EQ, case_insensitive) for v in expected_list
        )
        return hit if cmp.op == Op.IN else not hit

    expected = cmp.values[0]

    if cmp.op in (Op.CONTAINS, Op.NOT_CONTAINS):
        if isinstance(actual, (list, tuple, set)):
            hit = any(
                _compare_scalar(item, expected, Op.EQ, case_insensitive)
                for item in actual
            )
        elif isinstance(actual, dict):
            hit = str(expected) in actual
        elif actual is None:
            hit = False
        else:
            hit = _compare_scalar(actual, expected, Op.CONTAINS, case_insensitive)
        return hit if cmp.op == Op.CONTAINS else not hit

    if cmp.op in (Op.STARTSWITH, Op.ENDSWITH, Op.MATCHES):
        if actual is None:
            return False
        return _compare_scalar(actual, expected, cmp.op, case_insensitive)

    if not found:
        # Missing field: != holds (it is \"not equal\"), every other comparison fails
        return cmp.op == Op.NE

    return _compare_scalar(actual, expected, cmp.op, case_insensitive)


def evaluate(node: Any, fields: Dict[str, Any], case_insensitive: bool = False) -> bool:
    if isinstance(node, Cmp):
        return eval_cmp(node, fields, case_insensitive)
    if isinstance(node, Not):
        return not evaluate(node.item, fields, case_insensitive)
    if isinstance(node, Bool):
        results = [evaluate(item, fields, case_insensitive) for item in node.items]
        return all(results) if node.op == "and" else any(results)
    raise bad_request(f"Cannot evaluate node: {node!r}")


def build_fields(
    record: Dict[str, Any], metas: Optional[Dict[str, Any]] = None
) -> Dict[str, Any]:
    """Flatten a knowledge record into a searchable field map (built-in fields get a $ prefix)."""
    fields: Dict[str, Any] = {}
    for key in BUILTIN_FIELDS:
        if key in record:
            fields[f"${key}"] = record[key]
    for key, value in (metas or {}).items():
        fields[key] = value
    return fields


# --------------------------------------------------------------------------- #
# SQL pushdown - PostgreSQL target
# --------------------------------------------------------------------------- #
# WeKnora stores custom_metadata as a JSON column (jsonb in practice), so expression
# pushdown uses native JSON operators instead of exporting rows to Python first:
#
#   value at path a.b   custom_metadata::jsonb -> 'a' -> 'b'
#   text at path a.b    custom_metadata::jsonb -> 'a' ->> 'b'
#
# The compiled statement is deliberately a SUPERSET of the exact result: every row the
# caller asked for survives into Python, where evaluate() re-checks it with full type
# semantics (numbers vs strings vs dates vs lists). Two rules keep that guarantee:
#
#   1. Anything under a NOT node falls back to `1=1`; negating a superset would produce
#      a subset and silently drop matching rows. The exceptions are NOT EXISTS and
#      NOT CONTAINS, which are emitted as direct negative predicates instead.
#   2. Comparisons are widened (text form OR jsonb form) rather than narrowed.


def _quote_literal(value: str) -> str:
    return value.replace("'", "''")


def _meta_value_expr(column: str, parts: List[str]) -> str:
    chain = "".join(f" -> '{_quote_literal(p)}'" for p in parts)
    return f"({column}){chain}"


def _meta_text_expr(column: str, parts: List[str]) -> str:
    head = "".join(f" -> '{_quote_literal(p)}'" for p in parts[:-1])
    return f"({column}){head} ->> '{_quote_literal(parts[-1])}'"


def _like(value: Any) -> str:
    return str(value).replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def compile_sql(
    node: Any,
    *,
    metas_column: str = "custom_metadata",
    builtin_columns: Optional[Dict[str, str]] = None,
    case_insensitive: bool = False,
) -> Tuple[str, Dict[str, Any]]:
    """Compile an AST into a PostgreSQL WHERE fragment plus its bind parameters."""
    builtin_columns = builtin_columns or {}
    params: Dict[str, Any] = {}
    like_op = "ILIKE" if case_insensitive else "LIKE"

    def param(value: Any) -> str:
        key = f"p{len(params)}"
        params[key] = value
        return f":{key}"

    def _json_literal(value: Any) -> str:
        return json.dumps(value, ensure_ascii=False)

    def json_param(value: Any) -> str:
        key = f"p{len(params)}"
        params[key] = _json_literal(value)
        return f":{key}"

    def text_of(value: Any) -> Any:
        if isinstance(value, bool):
            return "true" if value else "false"
        if value is None:
            return ""
        return str(value)

    def resolve(name: str) -> Tuple[Optional[str], Optional[str], str]:
        """Return (text_expr, json_expr, kind); kind is meta | column."""
        if name.startswith("$"):
            column = builtin_columns.get(name[1:])
            return (column, None, "column") if column else (None, None, "missing")
        parts = [p for p in name.split(".") if p != ""]
        if not parts:
            return None, None, "missing"
        return (
            _meta_text_expr(metas_column, parts),
            _meta_value_expr(metas_column, parts),
            "meta",
        )

    def walk(n: Any) -> str:
        if isinstance(n, Bool):
            parts = [walk(i) for i in n.items]
            if not parts:
                return "1=1"
            joiner = " AND " if n.op == "and" else " OR "
            return "(" + joiner.join(parts) + ")"
        if isinstance(n, Not):
            item = n.item
            # Rule 1: never negate a widened predicate, except for the exact cases below
            if isinstance(item, Cmp) and item.op in (Op.EXISTS, Op.NOT_EXISTS):
                # Both mean "absent or empty", and that negation is exact, so it can be pushed down
                t, _j, kind = resolve(item.field)
                if not t:
                    return "1=1"
                return (
                    f"(COALESCE({t}, '') = '')" if kind == "meta" else f"({t} IS NULL)"
                )
            if isinstance(item, Cmp) and item.op == Op.NOT_CONTAINS and item.values:
                t, j, kind = resolve(item.field)
                if not t:
                    return "1=1"
                needle = param(f"%{_like(item.values[0])}%")
                if kind == "meta":
                    return f"(COALESCE(({j})::text, '') NOT {like_op} {needle} ESCAPE '\\')"
                return f"(COALESCE(({t})::text, '') NOT {like_op} {needle} ESCAPE '\\')"
            return "1=1"
        if not isinstance(n, Cmp):
            return "1=1"

        t, j, kind = resolve(n.field)
        if kind == "missing" or not t:
            return "1=1"

        if n.op == Op.EXISTS:
            return (
                f"(COALESCE({t}, '') <> '')" if kind == "meta" else f"({t} IS NOT NULL)"
            )
        if n.op == Op.NOT_EXISTS:
            return f"(COALESCE({t}, '') = '')" if kind == "meta" else f"({t} IS NULL)"

        if n.op in (Op.CONTAINS, Op.NOT_CONTAINS):
            if not n.values:
                return "1=1"
            needle = param(f"%{_like(n.values[0])}%")
            op_word = like_op if n.op == Op.CONTAINS else f"NOT {like_op}"
            if kind == "meta":
                return f"(COALESCE(({j})::text, '') {op_word} {needle} ESCAPE '\\')"
            return f"(COALESCE(({t})::text, '') {op_word} {needle} ESCAPE '\\')"

        if n.op in (Op.STARTSWITH, Op.ENDSWITH):
            if not n.values:
                return "1=1"
            pattern = param(
                f"{_like(n.values[0])}%"
                if n.op == Op.STARTSWITH
                else f"%{_like(n.values[0])}"
            )
            source = (
                f"COALESCE({t}, '')" if kind == "meta" else f"COALESCE(({t})::text, '')"
            )
            return f"({source} {like_op} {pattern} ESCAPE '\\')"

        if n.op == Op.MATCHES:
            return "1=1"  # regex dialects differ too much to push down safely

        if n.op in (Op.IN, Op.NOT_IN):
            raw = (
                n.values[0]
                if len(n.values) == 1 and isinstance(n.values[0], (list, tuple))
                else n.values
            )
            values = list(raw or [])
            if not values:
                return "1=1"
            text_items = ", ".join(param(text_of(v)) for v in values)
            if kind == "meta":
                json_items = ", ".join(
                    f"CAST({json_param(v)} AS jsonb)" for v in values
                )
                if n.op == Op.IN:
                    return f"(COALESCE({t}, '') IN ({text_items}) OR ({j}) IN ({json_items}))"
                return (
                    f"(COALESCE({t}, '') NOT IN ({text_items}) "
                    f"AND (({j}) IS NULL OR ({j}) NOT IN ({json_items})))"
                )
            op_word = "IN" if n.op == Op.IN else "NOT IN"
            null_guard = "" if n.op == Op.IN else f" OR {t} IS NULL"
            return f"(({t}) {op_word} ({text_items}){null_guard})"

        if len(n.values) != 1:
            return "1=1"
        value = n.values[0]
        sql_op = {
            Op.EQ: "=",
            Op.NE: "<>",
            Op.GT: ">",
            Op.GTE: ">=",
            Op.LT: "<",
            Op.LTE: "<=",
        }.get(n.op)
        if sql_op is None:
            return "1=1"

        if kind == "meta":
            if n.op == Op.EQ:
                return f"(COALESCE({t}, '') = {param(text_of(value))} OR ({j}) = CAST({json_param(value)} AS jsonb))"
            if n.op == Op.NE:
                return (
                    f"(COALESCE({t}, '') <> {param(text_of(value))} "
                    f"AND ({j}) IS DISTINCT FROM CAST({json_param(value)} AS jsonb))"
                )
            # Ordering: keep both interpretations (jsonb ordering and plain text) so the
            # SQL stays a superset of whatever evaluate() decides.
            return (
                f"(({j}) {sql_op} CAST({json_param(value)} AS jsonb) "
                f"OR COALESCE({t}, '') {sql_op} {param(text_of(value))})"
            )

        # Built-in columns use their native type; NULL never satisfies anything except !=
        null_guard = f" OR {t} IS NULL" if n.op == Op.NE else ""
        return f"(({t}) {sql_op} {param(value)}{null_guard})"

    sql = walk(node)
    return (sql or "1=1"), params


def describe(node: Any) -> Any:
    """Turn the AST into a JSON-serializable structure (used by /metas/parse for debugging)."""
    if isinstance(node, Cmp):
        return {
            "type": "cmp",
            "field": node.field,
            "op": node.op.value,
            "values": node.values,
        }
    if isinstance(node, Not):
        return {"type": "not", "item": describe(node.item)}
    if isinstance(node, Bool):
        return {"type": node.op, "items": [describe(i) for i in node.items]}
    return {"type": "unknown"}


def used_fields(node: Any) -> List[str]:
    out: List[str] = []

    def walk(n: Any) -> None:
        if isinstance(n, Cmp):
            out.append(n.field)
        elif isinstance(n, Not):
            walk(n.item)
        elif isinstance(n, Bool):
            for i in n.items:
                walk(i)

    walk(node)
    return out
