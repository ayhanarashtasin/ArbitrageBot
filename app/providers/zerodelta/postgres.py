"""Bounded, read-only PostgreSQL transport. Never owns the bot's persistence database."""

import asyncio
import logging
import sys
import time
from collections import Counter
from datetime import UTC, datetime, timedelta
from decimal import Decimal, InvalidOperation

from app.core.config import ZeroDeltaSettings

from .exceptions import (
    ZeroDeltaAccessError,
    ZeroDeltaConfigError,
    ZeroDeltaError,
    ZeroDeltaParseError,
    ZeroDeltaTimeoutError,
)

SUMMARY_SQL = """SELECT coin, exchange, avg_volume, avg_open_interest,
    avg_apr, avg_apr_24h, avg_apr_3d, avg_apr_7d, avg_apr_30d, avg_apr_90d
    FROM marts.fct_coin_exchange_summary ORDER BY coin, exchange"""
SYMBOL_SQL = """SELECT DISTINCT symbol FROM public.perp_market_data
    WHERE split_part(symbol, '/', 1) = %s AND exchange = %s
      AND datetime >= %s AND datetime <= %s ORDER BY symbol LIMIT 3"""
HISTORY_SQL = """WITH market_window AS MATERIALIZED (
    SELECT exchange, symbol, datetime, timestamp, fundingrate,
    "interval", markprice, volume, openinterest, nextfundingtime
    FROM public.perp_market_data
    WHERE split_part(symbol, '/', 1) = %s AND exchange = %s
      AND datetime >= %s AND datetime <= %s
    ) SELECT * FROM market_window WHERE symbol = %s
    ORDER BY datetime ASC, timestamp ASC LIMIT %s"""
SAMPLE_SQL = """SELECT exchange, symbol, datetime, timestamp, fundingrate,
    "interval", markprice, volume, openinterest, nextfundingtime
    FROM public.perp_market_data
    WHERE split_part(symbol, '/', 1) = %s AND exchange = %s
    ORDER BY datetime DESC LIMIT 5"""

log = logging.getLogger(__name__)


def number(value, *, nullable=False) -> Decimal | None:
    if nullable and value is None:
        return None
    try:
        if isinstance(value, bool) or value is None:
            raise InvalidOperation
        out = Decimal(str(value))
        if not out.is_finite():
            raise InvalidOperation
        return out
    except (InvalidOperation, ValueError):
        raise ZeroDeltaParseError("non-finite or missing numeric database value") from None


def utc(value: datetime) -> datetime:
    if not isinstance(value, datetime):
        raise ZeroDeltaParseError("database datetime is not a timestamp")
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


def epoch(value, unit: str) -> datetime | None:
    if value is None:
        return None
    try:
        seconds = number(value) / (1000 if unit == "milliseconds" else 1)
        return datetime.fromtimestamp(float(seconds), UTC)
    except (ValueError, OverflowError, OSError):
        raise ZeroDeltaParseError("database epoch is outside the supported range") from None


def hour(value: datetime) -> datetime:
    return utc(value).replace(minute=0, second=0, microsecond=0)


def missing_hours(timestamps, start: datetime, end: datetime) -> list[datetime]:
    """Exact absent UTC buckets, including both bounds; never manufactures observations."""
    present = {hour(at) for at in timestamps}
    start, end = hour(start), hour(end)
    return [at for n in range(int((end - start).total_seconds() // 3600) + 1)
            if (at := start + timedelta(hours=n)) not in present]


def next_refresh(value: datetime, minute: int = 5) -> datetime:
    value = utc(value)
    boundary = value.replace(minute=minute, second=0, microsecond=0)
    return boundary if boundary > value else boundary + timedelta(hours=1)


class PostgresClient:
    def __init__(self, settings: ZeroDeltaSettings):
        self.settings = settings
        self._sem = asyncio.Semaphore(settings.db_concurrency)
        self.last_query_ms: float | None = None
        self._counts = Counter()
        self._max_query_ms = 0.0

    async def query(self, sql: str, params=()) -> list[dict]:
        async with self._sem:
            if sys.platform == "win32" and not isinstance(asyncio.get_running_loop(), asyncio.SelectorEventLoop):
                # Uvicorn may use Proactor on Windows. libpq runs on a short-lived Selector loop off the engine loop.
                def run():
                    with asyncio.Runner(loop_factory=asyncio.SelectorEventLoop) as runner:
                        return runner.run(self._query(sql, params))
                return await asyncio.to_thread(run)
            return await self._query(sql, params)

    async def _query(self, sql: str, params=()) -> list[dict]:
        if self.settings.database_url is None:
            raise ZeroDeltaConfigError("ZERO_DELTA_DATABASE_URL is not configured")
        try:
            import psycopg
            from psycopg.conninfo import conninfo_to_dict
            from psycopg.rows import dict_row
        except ImportError:
            raise ZeroDeltaConfigError('Install the postgres extra: pip install -e ".[postgres]"') from None
        started = time.monotonic()
        operation = {SUMMARY_SQL: "summary", SYMBOL_SQL: "symbols", HISTORY_SQL: "history", SAMPLE_SQL: "sample"}.get(sql, "query")
        context = f"asset={params[0]} exchange={params[1]}" if operation in {"symbols", "history", "sample"} else ""
        outcome = "ok"
        self._counts["queries"] += 1
        try:
            async with asyncio.timeout(self.settings.db_connect_timeout_seconds
                                           + self.settings.db_query_timeout_seconds + 1):
                args = conninfo_to_dict(self.settings.database_url.get_secret_value())
                args.setdefault("sslmode", "require")
                args.update(connect_timeout=self.settings.db_connect_timeout_seconds,
                            options=("-c default_transaction_read_only=on -c statement_timeout="
                                     + str(int(self.settings.db_query_timeout_seconds * 1000))))
                async with (await psycopg.AsyncConnection.connect(**args) as conn,
                            conn.cursor(row_factory=dict_row) as cursor):
                    await cursor.execute(sql, params)
                    return await cursor.fetchall()
        except (TimeoutError, psycopg.errors.QueryCanceled):
            outcome = "timeout"
            self._counts["timeouts"] += 1
            raise ZeroDeltaTimeoutError("Zero Delta database query timed out") from None
        except psycopg.Error as exc:
            # Never expose libpq messages: they may contain connection strings or passwords.
            state = exc.sqlstate or "connection"
            outcome = state
            if state.startswith("28") or state == "42501":
                raise ZeroDeltaAccessError(f"Zero Delta database access denied ({state})") from None
            raise ZeroDeltaError(f"Zero Delta database request failed ({state})") from None
        except (ValueError, TypeError):
            outcome = "invalid_configuration"
            raise ZeroDeltaConfigError("Invalid Zero Delta database connection configuration") from None
        except asyncio.CancelledError:
            outcome = "cancelled"
            raise
        finally:
            self.last_query_ms = (time.monotonic() - started) * 1000
            self._max_query_ms = max(self._max_query_ms, self.last_query_ms)
            if outcome != "ok":
                self._counts["failed_queries"] += 1
            log.log(logging.INFO if outcome == "ok" else logging.WARNING,
                    "Zero Delta %s %s: %s in %.0fms", operation, context, outcome, self.last_query_ms)

    def diagnostics(self) -> dict:
        return {**dict(self._counts), "last_query_ms": self.last_query_ms,
                "max_query_ms": round(self._max_query_ms, 1),
                "statement_budget_ms": int(self.settings.db_query_timeout_seconds * 1000)}

    async def summary(self):
        return await self.query(SUMMARY_SQL)

    async def symbols(self, coin, exchange, start, end):
        return await self.query(SYMBOL_SQL, (coin, exchange, start.replace(tzinfo=None), end.replace(tzinfo=None)))

    async def history(self, coin, exchange, symbol, start, end):
        rows = await self.query(HISTORY_SQL, (coin, exchange, start.replace(tzinfo=None),
                                             end.replace(tzinfo=None), symbol, self.settings.db_max_rows_per_market + 1))
        if len(rows) > self.settings.db_max_rows_per_market:
            raise ZeroDeltaParseError("market history exceeds row budget; refusing truncated history")
        return rows

    async def aclose(self):
        pass  # connections are short-lived context managers, including during cancellation
