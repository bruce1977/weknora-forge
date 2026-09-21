"""PostgreSQL access layer shared by metadata search and purge.

Everything goes through one SQLAlchemy async engine so that connection pooling, timeouts
and error translation live in a single place. Callers receive an :class:`Executor` that
is either engine-bound (one implicit transaction per statement) or transaction-bound
(all statements inside one explicit transaction, which is what purge needs).

Because Forge talks to a database it does not own (and whose schema evolves between
WeKnora releases), every table/column reference can be resolved at runtime through
:meth:`Executor.columns` instead of being hard-coded.
"""

from __future__ import annotations

from contextlib import asynccontextmanager
from typing import Any, AsyncIterator, Dict, List, Mapping, Optional, Sequence, Set, Union

from sqlalchemy import bindparam, text
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine, create_async_engine

from ..config import Config
from ..errors import bad_request, database_error
from ..logging import get_logger

logger = get_logger(__name__)

Statement = Union[str, Any]  # str or sqlalchemy TextClause


def quote_ident(name: str) -> str:
    """Quote an identifier; also guards against config-driven SQL injection."""
    from ..config import is_identifier

    if not name or not is_identifier(name):
        raise bad_request(f"Unsafe SQL identifier: {name!r}", error_id="UNSAFE_IDENTIFIER")
    return f'"{name}"'


def _jsonable(value: Any) -> Any:
    from datetime import date, datetime
    from decimal import Decimal
    from uuid import UUID

    if isinstance(value, (datetime, date)):
        return value.isoformat()
    if isinstance(value, Decimal):
        return float(value)
    if isinstance(value, UUID):
        return str(value)
    if isinstance(value, (dict, list)):
        return value
    if isinstance(value, (bytes, bytearray)):
        return value.decode("utf-8", "replace")
    return value


class Executor:
    """Runs SQL against a connection or an engine."""

    def __init__(
        self,
        *,
        engine: Optional[AsyncEngine] = None,
        connection: Optional[AsyncConnection] = None,
        column_cache: Optional[Dict[str, Set[str]]] = None,
    ) -> None:
        if engine is None and connection is None:
            raise ValueError("Executor needs an engine or a connection")
        self._engine = engine
        self._connection = connection
        self._column_cache: Dict[str, Set[str]] = column_cache if column_cache is not None else {}

    def _target(self):
        return self._connection if self._connection is not None else self._engine

    @staticmethod
    def _stmt(sql: Statement, params: Optional[Mapping[str, Any]] = None):
        stmt = text(sql) if isinstance(sql, str) else sql
        if params:
            # Support Python sequences for `IN :name` clauses
            expanding = {}
            values: Dict[str, Any] = {}
            for key, value in params.items():
                if isinstance(value, (list, tuple, set)):
                    expanding[key] = bindparam(key, expanding=True)
                    values[key] = list(value)
                else:
                    values[key] = value
            if expanding:
                stmt = stmt.bindparams(*expanding.values())
            return stmt, values
        return stmt, dict(params or {})

    async def fetch(self, sql: Statement, params: Optional[Mapping[str, Any]] = None) -> List[Dict[str, Any]]:
        stmt, values = self._stmt(sql, params)
        try:
            result = await self._target().execute(stmt, values)
        except SQLAlchemyError as exc:
            raise database_error(f"Query failed: {exc}", str(exc)) from exc
        rows = result.mappings().all()
        return [{k: _jsonable(v) for k, v in row.items()} for row in rows]

    async def fetch_one(self, sql: Statement, params: Optional[Mapping[str, Any]] = None) -> Optional[Dict[str, Any]]:
        rows = await self.fetch(sql, params)
        return rows[0] if rows else None

    async def scalar(self, sql: Statement, params: Optional[Mapping[str, Any]] = None) -> Any:
        rows = await self.fetch(sql, params)
        if not rows:
            return None
        return next(iter(rows[0].values()), None)

    async def execute(self, sql: Statement, params: Optional[Mapping[str, Any]] = None) -> int:
        stmt, values = self._stmt(sql, params)
        try:
            result = await self._target().execute(stmt, values)
        except SQLAlchemyError as exc:
            raise database_error(f"Statement failed: {exc}", str(exc)) from exc
        return int(result.rowcount or 0)

    async def set_local(self, key: str, value: Any) -> None:
        await self.execute(f"SET LOCAL {key} = :value", {"value": value})

    async def columns(self, table: str) -> Set[str]:
        """Column names of a table, cached for the lifetime of the process."""
        if table in self._column_cache:
            return self._column_cache[table]
        rows = await self.fetch(
            "SELECT column_name FROM information_schema.columns WHERE table_name = :table",
            {"table": table},
        )
        names = {str(r["column_name"]) for r in rows}
        self._column_cache[table] = names
        if not names:
            logger.warning("table %s not found in information_schema", table)
        return names

    async def table_exists(self, table: str) -> bool:
        return bool(await self.columns(table))


class Database:
    """Owns the async engine and hands out Executors."""

    def __init__(self, config: Config) -> None:
        self.config = config
        self._engine: Optional[AsyncEngine] = None
        self._column_cache: Dict[str, Set[str]] = {}

    @property
    def dsn(self) -> str:
        return self.config.database.sqlalchemy_dsn

    @property
    def configured(self) -> bool:
        return self.config.database.configured

    def engine(self) -> AsyncEngine:
        if self._engine is None:
            if not self.configured:
                raise bad_request(
                    "No PostgreSQL connection configured: fill in the database section of config.json",
                    error_id="DB_NOT_CONFIGURED",
                )
            self._engine = create_async_engine(
                self.dsn,
                pool_size=self.config.database.pool_size,
                max_overflow=self.config.database.max_overflow,
                pool_pre_ping=True,
                connect_args=self._connect_args(),
            )
        return self._engine

    def _connect_args(self) -> Dict[str, Any]:
        args: Dict[str, Any] = {}
        timeout = self.config.database.statement_timeout_ms
        if timeout > 0:
            # asyncpg applies server_settings at connect time
            args["server_settings"] = {"statement_timeout": str(timeout)}
        sslmode = (self.config.database.sslmode or "").strip().lower()
        if sslmode:
            # asyncpg accepts the familiar sslmode strings for its own `ssl` argument
            args["ssl"] = sslmode
        return args

    def executor(self) -> Executor:
        return Executor(engine=self.engine(), column_cache=self._column_cache)

    @asynccontextmanager
    async def transaction(self) -> AsyncIterator[Executor]:
        async with self.engine().begin() as conn:
            yield Executor(connection=conn, column_cache=self._column_cache)

    async def close(self) -> None:
        if self._engine is not None:
            await self._engine.dispose()
            self._engine = None

    async def ping(self) -> bool:
        try:
            row = await self.executor().scalar("SELECT 1")
            return row == 1
        except Exception as exc:  # noqa: BLE001
            logger.warning("database ping failed: %s", exc)
            return False


# --------------------------------------------------------------------------- #
# Test double
# --------------------------------------------------------------------------- #
class FakeExecutor(Executor):
    """Records SQL instead of running it; rows are returned from a scripted queue.

    Used by the unit tests - the CI environment has no PostgreSQL.
    """

    def __init__(self, rows: Optional[Sequence[Dict[str, Any]]] = None, columns: Optional[Dict[str, Set[str]]] = None):
        super().__init__(engine=object())  # type: ignore[arg-type]
        self.statements: List[str] = []
        self.params: List[Dict[str, Any]] = []
        self.rows = list(rows or [])
        self._column_cache = columns or {}

    async def fetch(self, sql, params=None):  # type: ignore[override]
        self.statements.append(str(sql))
        self.params.append(dict(params or {}))
        return [{k: _jsonable(v) for k, v in row.items()} for row in self.rows]

    async def execute(self, sql, params=None):  # type: ignore[override]
        self.statements.append(str(sql))
        self.params.append(dict(params or {}))
        return len(self.rows)

    async def set_local(self, key, value):
        self.statements.append(f"SET LOCAL {key} = :value")
        self.params.append({"value": value})


__all__ = [
    "Database",
    "Executor",
    "FakeExecutor",
    "Statement",
    "quote_ident",
]
