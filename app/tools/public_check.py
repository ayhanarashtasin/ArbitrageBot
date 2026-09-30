"""Read-only connector smoke check; uses public data, never credentials, discovery or orders.

    python -m app.tools.public_check Bitget OKX Gate "MEXC USDT" "BloFin USDT"
"""

import argparse
import asyncio
import json

from app.exchanges.registry import binding_for


async def check(names):
    failed = False
    for name in names:
        binding = binding_for(name)
        if binding is None:
            print(json.dumps({"venue": name, "error": "unsupported"}), flush=True)
            failed = True
            continue
        adapter = binding.factory(timeout=8, max_retries=1)
        try:
            meta = await adapter.resolve_symbol("BTC", binding.quote, binding.settle)
            funding = await adapter.get_funding_info(meta.symbol)
            book = await adapter.get_order_book(meta.symbol)
            candles = await adapter.get_candles(meta.symbol, "1m", 1500)
            print(json.dumps({"venue": name, "symbol": meta.symbol, "base_step": str(meta.qty_step),
                              "funding_hours": str(funding.interval_hours), "book_bid_levels": len(book.bids),
                              "closed_1m_candles": len(candles),
                              "coverage_minutes": (candles[-1].open_time - candles[0].open_time).total_seconds() / 60
                              if candles else 0}), flush=True)
        except Exception as exc:
            failed = True
            print(json.dumps({"venue": name, "error": str(exc)}), flush=True)
        finally:
            await adapter.close()
        await asyncio.sleep(0.3)
    return int(failed)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("venues", nargs="*", default=["Bitget", "OKX", "Gate", "MEXC USDT", "BloFin USDT"])
    args = parser.parse_args()
    raise SystemExit(asyncio.run(check(args.venues)))


if __name__ == "__main__":
    main()
