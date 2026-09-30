"""Stock earnings blackout. The task spec: "Avoid opening trades within 2-3 days of earnings dates of stocks.
Crypto pairs does not have condition."

What counts as a stock comes from the venues themselves, never from the ticker text (SPX on Binance is a meme coin,
not the S&P 500):
  Binance underlyingType  COIN | EQUITY | HK_EQUITY | KR_EQUITY | CN_EQUITY | COMMODITY | INDEX | PREMARKET (verified live)
  Trade.xyz (HIP-3 dex)   TRADFI: stocks, indices and commodities, with no finer label

Earnings dates come from two sources:
  automatic  Nasdaq's public earnings calendar, GET https://api.nasdaq.com/api/calendar/earnings?date=YYYY-MM-DD
             (no key; verified live 2026-09-30: data.rows[].symbol for companies reporting that day, rows null on
             days without reports; an empty body happens now and then and is retried once). US listings only.
  file       EARNINGS_CALENDAR_FILE, CSV rows "SYMBOL,YYYY-MM-DD" ('#' starts a comment), for dates the calendar cannot
             know (Korean, Hong Kong, Chinese listings) or to add a date by hand.

Fail closed (project.md: when information is missing, no trade):
  - a US stock, or a TradFi contract with no finer label, is blocked while any day of the window cannot be read;
  - a non-US stock is blocked unless the file gives its next earnings date and that date is outside the window.
ETFs are labelled EQUITY by Binance but report no earnings, so the calendar never lists them and they pass.
Commodities, indices and pre-IPO contracts have no earnings and pass.
"""

import asyncio
import csv
import logging
from collections.abc import Awaitable, Callable, Iterable
from datetime import date, datetime, timedelta
from pathlib import Path

import httpx

log = logging.getLogger(__name__)

CRYPTO = "crypto"
US_EQUITY = "us_equity"
OTHER_EQUITY = "other_equity"  # listed outside the US: the automatic calendar does not cover it
TRADFI = "tradfi"  # a stock, index or commodity without a finer label: checked against the calendar
NO_EARNINGS = "no_earnings"  # commodity, index, pre-IPO

NASDAQ_URL = "https://api.nasdaq.com/api/calendar/earnings"
_HEADERS = {  # the endpoint answers browsers only; this is a plain public read, no key or cookie
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126 Safari/537.36",
    "Accept": "application/json",
}
CACHE_SECONDS = 6 * 3600
RETRY_FAILED_AFTER_SECONDS = 300


def asset_class(underlying_types: Iterable[str | None]) -> str:
    """Asset class from the venues' own labels. No label anywhere = crypto (the default on crypto venues)."""
    types = {t.upper() for t in underlying_types if t}
    if not types:
        return CRYPTO
    if any(t.endswith("_EQUITY") for t in types):
        return OTHER_EQUITY
    if "EQUITY" in types:
        return US_EQUITY
    if types & {"COMMODITY", "INDEX", "PREMARKET"}:
        return NO_EARNINGS
    if "TRADFI" in types:
        return TRADFI  # also when another venue says COIN: a ticker clash is treated with caution
    return CRYPTO


class CalendarUnavailable(Exception):
    pass


async def nasdaq_day(day: date, client: httpx.AsyncClient) -> set[str]:
    """Symbols reporting on `day`. Raises CalendarUnavailable when the answer cannot be read."""
    last_error = None
    for attempt in range(2):
        try:
            r = await client.get(NASDAQ_URL, params={"date": day.isoformat()}, headers=_HEADERS)
            r.raise_for_status()
            body = r.json()
            data = body["data"]
            if not isinstance(data, dict) or "rows" not in data:
                raise ValueError("missing earnings rows")
            rows = data["rows"] or []
            return {str(row["symbol"]).strip().upper() for row in rows if row.get("symbol")}
        except (httpx.HTTPError, ValueError, KeyError, TypeError, AttributeError) as exc:
            last_error = exc
            if attempt == 0:
                await asyncio.sleep(1)
    raise CalendarUnavailable(f"{day}: {type(last_error).__name__}")


class EarningsCalendar:
    def __init__(self, *, blackout_days: int, now: Callable[[], datetime], auto_fetch: bool = True,
                 file_path: str | None = None,
                 fetch_day: Callable[[date], Awaitable[set[str]]] | None = None):
        self.blackout_days = blackout_days
        self._now = now
        self.auto_fetch = auto_fetch
        self.file_path = Path(file_path) if file_path else None
        self._fetch_day = fetch_day
        self._client: httpx.AsyncClient | None = None
        self._days: dict[date, tuple[datetime, set[str] | None]] = {}  # day -> (fetched at, symbols or None = failed)
        self._file: dict[str, list[date]] = {}
        self._file_mtime: float | None = None

    async def aclose(self) -> None:
        if self._client is not None:
            await self._client.aclose()

    # ---- sources ----------------------------------------------------------------------------------
    def _load_file(self) -> dict[str, list[date]]:
        if self.file_path is None or not self.file_path.exists():
            return {}
        mtime = self.file_path.stat().st_mtime
        if mtime == self._file_mtime:
            return self._file
        dates: dict[str, list[date]] = {}
        with self.file_path.open(encoding="utf-8") as fh:
            for row in csv.reader(fh):
                if not row or row[0].strip().startswith("#") or len(row) < 2:
                    continue
                try:
                    dates.setdefault(row[0].strip().upper(), []).append(date.fromisoformat(row[1].strip()))
                except ValueError:
                    log.warning("earnings file: skipping bad row %r", row)
        self._file, self._file_mtime = dates, mtime
        return dates

    async def _day(self, day: date) -> set[str] | None:
        now = self._now()
        cached = self._days.get(day)
        if cached is not None:
            at, symbols = cached
            age = (now - at).total_seconds()
            if (symbols is not None and age < CACHE_SECONDS) or (symbols is None and age < RETRY_FAILED_AFTER_SECONDS):
                return symbols
        try:
            if self._fetch_day is not None:
                symbols = await self._fetch_day(day)
            else:
                if self._client is None:
                    self._client = httpx.AsyncClient(timeout=15.0)
                symbols = await nasdaq_day(day, self._client)
        except Exception as exc:  # noqa: BLE001 - any failure means "unknown", and unknown blocks stocks
            log.warning("earnings calendar %s unavailable: %s", day, exc)
            symbols = None
        self._days[day] = (now, symbols)
        return symbols

    # ---- the rule ---------------------------------------------------------------------------------
    def window(self) -> list[date]:
        today = self._now().date()
        n = self.blackout_days
        return [today + timedelta(days=k) for k in range(-n, n + 1)]

    async def blackout(self, asset: str, cls: str) -> str | None:
        """None when a trade may open; otherwise the plain reason it may not."""
        if cls in (CRYPTO, NO_EARNINGS):
            return None
        symbol = asset.upper()
        window = self.window()
        known = sorted(self._load_file().get(symbol, []))
        hit = [d for d in known if window[0] <= d <= window[-1]]
        if hit:
            return f"{symbol} reports earnings on {hit[0]} (within {self.blackout_days} days)"
        known_later = any(d > window[-1] for d in known)
        if cls == OTHER_EQUITY:
            if known_later:
                return None
            return (f"{symbol} is a non-US stock and its earnings date is unknown: "
                    "add 'SYMBOL,YYYY-MM-DD' to EARNINGS_CALENDAR_FILE")
        if not self.auto_fetch:
            return None if known_later else f"{symbol}: earnings calendar is off and no date is on file"
        days = await asyncio.gather(*(self._day(d) for d in window))
        for d, symbols in zip(window, days):
            if symbols is not None and symbol in symbols:
                return f"{symbol} reports earnings on {d} (within {self.blackout_days} days)"
        if any(symbols is None for symbols in days):
            return f"{symbol}: earnings calendar unavailable, so a stock cannot be cleared (fail closed)"
        return None
