"""Re-capture the trimmed REAL public-endpoint responses used by tests/exchanges.

Run manually (needs network): python tests/fixtures/exchanges/build_fixtures.py

Every file is a real response from the venue's public endpoint (no keys, no personal data). The only
edits are trimming: fewer instruments / levels / rows, and (Bybit pagination only) the last captured
page has its nextPageCursor blanked so the chain terminates. Values are never altered.
Captured for the committed fixtures: 2026-09-29 ~15:30-15:45 UTC.
"""

import json
import time
from pathlib import Path

import httpx

ROOT = Path(__file__).parent
BINANCE = "https://fapi.binance.com"
BYBIT = "https://api.bybit.com"
HYPERLIQUID = "https://api.hyperliquid.xyz"


def _no_floats(text: str):
    """Parse JSON refusing floats: these venues send every decimal as a string; keep it that way."""
    def refuse(token):
        raise ValueError(f"unexpected JSON float {token}")
    return json.loads(text, parse_float=refuse)


def save(venue: str, name: str, data) -> None:
    folder = ROOT / venue
    folder.mkdir(parents=True, exist_ok=True)
    (folder / name).write_text(json.dumps(data, indent=1, ensure_ascii=False) + "\n", encoding="utf-8")
    print(f"wrote {venue}/{name}")


def fetch(client: httpx.Client, method: str, url: str, **kw):
    for attempt in range(6):  # Binance from some networks times out intermittently
        try:
            resp = client.request(method, url, **kw)
            resp.raise_for_status()
            return _no_floats(resp.text)
        except (httpx.TransportError, httpx.HTTPStatusError):
            time.sleep(1 + attempt)
    raise RuntimeError(f"could not fetch {url}")


def binance(client: httpx.Client) -> None:
    keep = ["BTCUSDT", "ETHUSDT", "SOLUSDT", "BTCUSDC", "ETHUSDC", "1000PEPEUSDT", "TSLAUSDT", "ETHBTC",
            "BTCUSDT_261225", "OMGUSDT", "LPTUSDT", "ONEUSDT"]
    info = fetch(client, "GET", f"{BINANCE}/fapi/v1/exchangeInfo")
    info["symbols"] = [s for s in info["symbols"] if s["symbol"] in keep]
    info["assets"] = [a for a in info["assets"] if a["asset"] in ("USDT", "USDC", "BTC")]
    save("binance", "exchange_info.json", info)
    funding = fetch(client, "GET", f"{BINANCE}/fapi/v1/fundingInfo")
    save("binance", "funding_info.json", [f for f in funding if f["symbol"] in keep])
    save("binance", "premium_index_btcusdt.json",
         fetch(client, "GET", f"{BINANCE}/fapi/v1/premiumIndex", params={"symbol": "BTCUSDT"}))
    save("binance", "funding_rate_btcusdt.json",
         fetch(client, "GET", f"{BINANCE}/fapi/v1/fundingRate", params={"symbol": "BTCUSDT", "limit": 10}))
    save("binance", "depth_btcusdt.json",
         fetch(client, "GET", f"{BINANCE}/fapi/v1/depth", params={"symbol": "BTCUSDT", "limit": 20}))
    save("binance", "ticker_24hr_btcusdt.json",
         fetch(client, "GET", f"{BINANCE}/fapi/v1/ticker/24hr", params={"symbol": "BTCUSDT"}))
    save("binance", "book_ticker_btcusdt.json",
         fetch(client, "GET", f"{BINANCE}/fapi/v1/ticker/bookTicker", params={"symbol": "BTCUSDT"}))
    save("binance", "klines_btcusdt_1m.json",
         fetch(client, "GET", f"{BINANCE}/fapi/v1/klines",
               params={"symbol": "BTCUSDT", "interval": "1m", "limit": 6}))
    save("binance", "server_time.json", fetch(client, "GET", f"{BINANCE}/fapi/v1/time"))


def bybit(client: httpx.Client) -> None:
    keep = ["BTCUSDT", "BTCPERP", "ETHUSDT", "ETHPERP", "SOLUSDT", "SOLPERP", "1000PEPEUSDT",
            "BTCUSDT-25DEC26"]
    full = fetch(client, "GET", f"{BYBIT}/v5/market/instruments-info", params={"category": "linear", "limit": 1000})
    rows = [r for r in full["result"]["list"] if r["symbol"] in keep]
    four_hour = next(r for r in full["result"]["list"] if r["fundingInterval"] == 240 and r["symbol"].endswith("USDT"))
    stock = next(r for r in full["result"]["list"] if r.get("symbolType") == "stock")
    full["result"]["list"] = rows + [four_hour, stock]
    save("bybit", "instruments_info.json", full)
    cursor = ""
    for page in (1, 2, 3):  # real cursor chain: [BTCPERP, BTCUSD(inverse)], [BTCUSDH27, BTCUSDT], [futures...]
        params = {"category": "linear", "baseCoin": "BTC", "limit": 2}
        if cursor:
            params["cursor"] = cursor
        body = fetch(client, "GET", f"{BYBIT}/v5/market/instruments-info", params=params)
        cursor = body["result"]["nextPageCursor"]
        if page == 3:
            body["result"]["nextPageCursor"] = ""
        save("bybit", f"instruments_page{page}.json", body)
    save("bybit", "tickers_btcusdt.json",
         fetch(client, "GET", f"{BYBIT}/v5/market/tickers", params={"category": "linear", "symbol": "BTCUSDT"}))
    save("bybit", "orderbook_btcusdt.json",
         fetch(client, "GET", f"{BYBIT}/v5/market/orderbook",
               params={"category": "linear", "symbol": "BTCUSDT", "limit": 20}))
    save("bybit", "funding_history_btcusdt.json",
         fetch(client, "GET", f"{BYBIT}/v5/market/funding/history",
               params={"category": "linear", "symbol": "BTCUSDT", "limit": 10}))
    save("bybit", "kline_btcusdt_1m.json",
         fetch(client, "GET", f"{BYBIT}/v5/market/kline",
               params={"category": "linear", "symbol": "BTCUSDT", "interval": "1", "limit": 6}))
    save("bybit", "server_time.json", fetch(client, "GET", f"{BYBIT}/v5/market/time"))


def _trim_meta_and_ctxs(data, coins: list[str]):
    meta, ctxs = data
    picked = [(u, c) for u, c in zip(meta["universe"], ctxs, strict=True) if u["name"] in coins]
    meta = dict(meta)
    meta["universe"] = [u for u, _ in picked]
    ids = {u.get("marginTableId") for u in meta["universe"]}
    meta["marginTables"] = [t for t in meta["marginTables"] if t[0] in ids]
    return [meta, [c for _, c in picked]]


def hyperliquid(client: httpx.Client) -> None:
    def info(body):
        return fetch(client, "POST", f"{HYPERLIQUID}/info", json=body)

    now = int(time.time() * 1000)
    core = info({"type": "metaAndAssetCtxs"})
    delisted = next(u["name"] for u in core[0]["universe"] if u.get("isDelisted"))
    save("hyperliquid", "meta_and_asset_ctxs.json",
         _trim_meta_and_ctxs(core, ["BTC", "ETH", "SOL", "kPEPE", delisted]))
    spot = info({"type": "spotMeta"})
    spot["tokens"] = [t for t in spot["tokens"] if t["index"] in (0, 360)]
    spot["universe"] = []
    save("hyperliquid", "spot_meta.json", spot)
    save("hyperliquid", "meta_and_asset_ctxs_xyz.json",
         _trim_meta_and_ctxs(info({"type": "metaAndAssetCtxs", "dex": "xyz"}), ["xyz:XYZ100", "xyz:TSLA", "xyz:GOLD"]))
    km = info({"type": "metaAndAssetCtxs", "dex": "km"})
    km_coins = [km[0]["universe"][0]["name"], "km:GOLD"]
    save("hyperliquid", "meta_and_asset_ctxs_km.json", _trim_meta_and_ctxs(km, km_coins))
    save("hyperliquid", "l2book_btc.json", info({"type": "l2Book", "coin": "BTC"}))
    save("hyperliquid", "l2book_xyz_tsla.json", info({"type": "l2Book", "coin": "xyz:TSLA"}))
    hour = 3_600_000
    save("hyperliquid", "funding_history_btc.json",
         info({"type": "fundingHistory", "coin": "BTC", "startTime": now - 10 * hour}))
    save("hyperliquid", "funding_history_xyz_tsla.json",
         info({"type": "fundingHistory", "coin": "xyz:TSLA", "startTime": now - 10 * hour}))
    for coin, name in (("BTC", "candles_btc_1m.json"), ("xyz:TSLA", "candles_xyz_tsla_1m.json")):
        save("hyperliquid", name, info({"type": "candleSnapshot", "req": {
            "coin": coin, "interval": "1m", "startTime": now - 5 * 60_000, "endTime": now}}))
    save("hyperliquid", "exchange_status.json", info({"type": "exchangeStatus"}))


if __name__ == "__main__":
    with httpx.Client(timeout=15) as http:
        binance(http)
        bybit(http)
        hyperliquid(http)
