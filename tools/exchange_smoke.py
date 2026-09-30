"""Live smoke test of the direct-venue PUBLIC market-data adapters (project.md Phase 3).

Usage (from the repo root):
    python tools/exchange_smoke.py [--base BTC] [--hip3-dex xyz --hip3-base TSLA] [--no-hip3]

For each adapter it prints: the symbol resolved from the venue's own instrument list, the instrument
rules, funding (rate as a fraction and in %, interval, next funding time, predicted flag), best
bid/ask, order-book age, the last closed 1m candle and the venue server clock offset.

Public endpoints only: no API key, no signature, no account or order call. Nothing is written to disk
and nothing secret exists to print. A venue that is unreachable or refuses the request (for example
HTTP 403/451 geo-blocking) is reported as FAIL and does not stop the other venues.
"""

import argparse
import asyncio
import sys
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.exchanges.base import ExchangeAdapter, ExchangeError  # noqa: E402
from app.exchanges.binance import BinanceUsdmAdapter  # noqa: E402
from app.exchanges.bybit import BybitLinearAdapter  # noqa: E402
from app.exchanges.hyperliquid import HyperliquidAdapter  # noqa: E402


def pct(fraction: Decimal) -> str:
    return f"{fraction * 100:.6f}%"


async def smoke(adapter: ExchangeAdapter, base: str, quote: str, settle: str) -> bool:
    print(f"\n=== {adapter.name}  ({base}/{quote}:{settle}) ===")
    try:
        meta = await adapter.resolve_symbol(base, quote, settle)
        print(f"symbol        : {meta.symbol}  (base={meta.base} quote={meta.quote} settle={meta.settle} "
              f"linear={meta.is_linear} tradable={meta.is_tradable})")
        print(f"rules         : tick={meta.tick_size} step={meta.qty_step} min_qty={meta.min_qty} "
              f"max_qty={meta.max_qty} min_notional={meta.min_notional} multiplier={meta.contract_multiplier} "
              f"max_leverage={meta.max_leverage}")
        funding = await adapter.get_funding_info(meta.symbol)
        next_time = funding.next_funding_time.isoformat() if funding.next_funding_time else "n/a"
        print(f"funding       : rate={funding.funding_rate} ({pct(funding.funding_rate)}) per "
              f"{funding.interval_hours}h  per-hour={pct(funding.rate_per_hour)}  next={next_time}  "
              f"predicted={funding.is_predicted}")
        print(f"                mark={funding.mark_price} index={funding.index_price}")
        book = await adapter.get_order_book(meta.symbol, depth=20)
        source = book.source_time.isoformat() if book.source_time else "none (received time used)"
        print(f"book          : bid={book.best_bid} ask={book.best_ask} levels={len(book.bids)}x{len(book.asks)} "
              f"age={book.age_ms():.0f}ms source_time={source}")
        candles = await adapter.get_candles(meta.symbol, "1m", limit=3)
        last = candles[-1]
        print(f"candle (1m)   : open_time={last.open_time.isoformat()} o={last.open} h={last.high} l={last.low} "
              f"c={last.close} volume={last.volume} ({len(candles)} returned, oldest first)")
        history = await adapter.get_funding_history(meta.symbol, limit=3)
        print("funding hist  : " + ", ".join(f"{t:%m-%d %H:%M}={r}" for t, r in history))
        ticker = await adapter.get_ticker(meta.symbol)
        print(f"ticker        : bid={ticker.bid} ask={ticker.ask} last={ticker.last} mark={ticker.mark} "
              f"volume_24h_usd={ticker.volume_24h_usd}")
        server_time = await adapter.get_server_time()
        skew_ms = (datetime.now(UTC) - server_time).total_seconds() * 1000
        print(f"server time   : {server_time.isoformat()}  (local - server = {skew_ms:.0f}ms)")
        print("RESULT        : OK")
        return True
    except ExchangeError as exc:
        print(f"RESULT        : FAIL  {type(exc).__name__}: {exc}")
        return False


async def main(args: argparse.Namespace) -> int:
    targets = [
        (BinanceUsdmAdapter(), args.base, "USDT", "USDT"),
        (BybitLinearAdapter(), args.base, "USDT", "USDT"),
        (HyperliquidAdapter(), args.base, "USDC", "USDC"),
    ]
    if not args.no_hip3:
        targets.append((HyperliquidAdapter(dex=args.hip3_dex), args.hip3_base, "USDC", "USDC"))
    results = []
    try:
        for adapter, base, quote, settle in targets:
            results.append(await smoke(adapter, base, quote, settle))
    finally:
        for adapter, *_ in targets:
            await adapter.close()
    print(f"\n{sum(results)}/{len(results)} adapters OK")
    return 0 if all(results) else 1


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--base", default="BTC", help="base asset to resolve on Binance / Bybit / Hyperliquid")
    parser.add_argument("--hip3-dex", default="xyz", help="Hyperliquid HIP-3 dex name")
    parser.add_argument("--hip3-base", default="TSLA", help="base asset to resolve on the HIP-3 dex")
    parser.add_argument("--no-hip3", action="store_true", help="skip the HIP-3 adapter")
    sys.exit(asyncio.run(main(parser.parse_args())))
