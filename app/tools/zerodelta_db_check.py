"""Read-only upstream preflight. No trading runtime, local database, or website is started."""

import argparse
import asyncio
import getpass
import json
import sys
from datetime import timedelta
from pathlib import Path

from pydantic import SecretStr

from app.core.config import ZeroDeltaSettings
from app.providers.zerodelta.exceptions import ZeroDeltaError
from app.providers.zerodelta.postgres import (
    HISTORY_SQL,
    SAMPLE_SQL,
    PostgresClient,
    missing_hours,
)
from app.providers.zerodelta.provider import ZeroDeltaProvider


async def inspect(settings, asset, exchange, days, direct_check=False):
    client = PostgresClient(settings)
    report = {"source": "postgres", "missing_contract": settings.missing_database_contract()}
    report["columns"] = await client.query("""SELECT table_schema, table_name, column_name, data_type
        FROM information_schema.columns WHERE (table_schema = 'marts' AND table_name = 'fct_coin_exchange_summary')
        OR (table_schema = 'public' AND table_name = 'perp_market_data') ORDER BY table_schema, ordinal_position""")
    report["indexes"] = await client.query("""SELECT indexname, indexdef FROM pg_indexes
        WHERE schemaname = 'public' AND tablename = 'perp_market_data'""")
    report["connection_limits"] = await client.query("""SELECT current_setting('statement_timeout') AS statement_timeout,
        current_setting('transaction_read_only') AS read_only, current_setting('TimeZone') AS session_timezone""")
    summaries = await client.summary()
    report["summary_rows"] = len(summaries)
    report["summary_sample"] = [r for r in summaries if str(r["coin"]).upper() == asset and r["exchange"] == exchange][:2]
    report["raw_sample"] = await client.query(SAMPLE_SQL, (asset, exchange))
    if direct_check and report["raw_sample"]:
        from app.exchanges.base import ExchangeError
        from app.exchanges.registry import binding_for
        from app.providers.zerodelta.venues import database_venue_name
        from app.strategy.instruments import parse_market_identifier
        binding = binding_for(database_venue_name(exchange, settings.db_venue_names))
        instrument = parse_market_identifier(report["raw_sample"][0]["symbol"])
        if binding is not None and instrument is not None:
            adapter = binding.factory()
            try:
                meta = await adapter.resolve_symbol(instrument.base, instrument.quote, instrument.settle)
                funding, settlements, ticker = await asyncio.gather(adapter.get_funding_info(meta.symbol),
                    adapter.get_funding_history(meta.symbol, 4), adapter.get_ticker(meta.symbol))
                report["direct_venue"] = {"funding": funding.model_dump(mode="json"),
                                          "settlements": settlements, "ticker": ticker.model_dump(mode="json")}
            except (ExchangeError, ValueError) as exc:
                report["direct_venue"] = {"error": str(exc)}
            finally:
                await adapter.close()
    report["refresh_verified"] = False
    report["note"] = "No refresh watermark is present in the supplied schema. Sampling does not prove units or refresh completion."
    if not report["missing_contract"]:
        provider = ZeroDeltaProvider(settings, postgres_client=client)
        await provider.refresh_summary()
        analysis = await provider.fetch_asset(asset, keep_days=days, exchanges=[exchange])
        report["analysis"] = {"asset": analysis.asset, "analysis_end": analysis.analysis_end,
            "markets": [m.model_dump(mode="json") for m in analysis.markets],
            "issues": [i.model_dump(mode="json") for i in analysis.issues],
            "samples": {venue: len(points) for venue, points in analysis.histories.items()},
            "missing_hours_7d": {venue: missing_hours((o.timestamp for o in points),
                analysis.analysis_end - timedelta(days=7), analysis.analysis_end)
                for venue, points in analysis.histories.items()},
            "missing_hours_retained_window": {venue: missing_hours((o.timestamp for o in points),
                analysis.analysis_end - timedelta(days=days), analysis.analysis_end)
                for venue, points in analysis.histories.items()}}
        report["summary_comparison"] = []
        for market in analysis.markets:
            snapshot = market.snapshot
            points = [o for o in analysis.histories[snapshot.venue_name]
                      if analysis.analysis_end - timedelta(days=7) <= o.timestamp <= analysis.analysis_end]
            summary_row = next((m for m in provider.summary_batch.markets if m.coin == asset
                                and m.exchange == snapshot.exchange_key), None)
            report["summary_comparison"].append({"exchange": snapshot.exchange_key,
                "raw_mean_7d_apr": sum(o.funding_apr for o in points) / len(points) if points else None,
                "summary_7d_apr": summary_row.avg_apr_7d if summary_row else None})
        report["history_query_ms"] = client.last_query_ms
        if analysis.markets:
            symbol = analysis.markets[0].snapshot.market_identifier
            end = (analysis.analysis_end + timedelta(hours=1) - timedelta(microseconds=1)).replace(tzinfo=None)
            start = (analysis.analysis_end - timedelta(days=days)).replace(tzinfo=None)
            params = (asset, exchange, start, end, symbol, settings.db_max_rows_per_market + 1)
            report["query_plan"] = await client.query("EXPLAIN (FORMAT JSON) " + HISTORY_SQL, params)
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--asset", default="BTC")
    parser.add_argument("--exchange", default="binance_usdt")
    parser.add_argument("--days", type=int, choices=(7, 14), default=14)
    parser.add_argument("--prompt-url", action="store_true", help="read the URL privately instead of from the environment")
    parser.add_argument("--direct-check", action="store_true", help="also read this market's public venue funding/settlements")
    parser.add_argument("--output", type=Path, help="save the credential-free report in an existing directory")
    args = parser.parse_args()
    if args.output and not args.output.parent.is_dir():
        parser.error("the output directory must already exist")
    settings = ZeroDeltaSettings(source="postgres")
    if args.prompt_url:
        settings = settings.model_copy(update={"database_url": SecretStr(getpass.getpass("PostgreSQL URL: "))})
    try:
        # libpq async needs a Selector loop on Windows, including Python 3.14.
        factory = asyncio.SelectorEventLoop if sys.platform == "win32" else None
        with asyncio.Runner(loop_factory=factory) as runner:
            report = runner.run(inspect(settings, args.asset.upper(), args.exchange, args.days, args.direct_check))
        text = json.dumps(report, default=str, indent=2)
        if args.output:
            args.output.write_text(text + "\n", encoding="utf-8")
            print(f"Read-only report saved to {args.output}")
        else:
            print(text)
    except ZeroDeltaError as exc:
        sys.exit(f"Zero Delta database: {exc}")


if __name__ == "__main__":
    main()
