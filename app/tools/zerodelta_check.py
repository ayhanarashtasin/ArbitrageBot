"""Diagnostics: what does Zero Delta report for one asset, and which markets pass the locked filters?

    python -m app.tools.zerodelta_check BTC             # live, needs ZERO_DELTA_SESSION_COOKIE + USER_ID
    python -m app.tools.zerodelta_check BTC --offline   # uses tests/fixtures/zerodelta/btc_dash_response.json
"""

import argparse
import asyncio
import json
import sys
from pathlib import Path

from app.core.config import ZeroDeltaSettings
from app.providers.zerodelta import AssetAnalysis, ZeroDeltaError, ZeroDeltaProvider

FIXTURES = Path(__file__).resolve().parents[2] / "tests" / "fixtures" / "zerodelta"


def render(analysis: AssetAnalysis) -> str:
    head = (
        f"{'venue':24} {'type':7} {'market id':16} {'funding%':>10} {'int h':>5} {'%/h':>10} "
        f"{'avg24h vol $M':>13} {'hist h':>7} {'obs':>4} {'vol ok':>6} {'hist ok':>7}"
    )
    lines = [f"{analysis.asset}  received {analysis.received_at:%Y-%m-%d %H:%M:%S}Z", head, "-" * len(head)]
    for m in sorted(analysis.markets, key=lambda m: m.snapshot.venue_name.lower()):
        s, c = m.snapshot, m.coverage
        lines.append(
            f"{s.venue_name[:24]:24} {s.venue_type.value + ("" if m.venue_type_verified or not m.venue_type_known else "*"):7} {s.market_identifier[:16]:16} "
            f"{s.funding_rate_raw:>10.4f} {s.funding_interval_hours:>5.0f} "
            f"{s.normalized_funding_rate_per_hour:>10.5f} {s.avg_24h_volume_usd / 1_000_000:>13.2f} "
            f"{(c.coverage_hours if c else 0):>7.0f} {(c.observation_count if c else 0):>4} "
            f"{'yes' if m.volume_ok else 'NO':>6} {'yes' if m.history_ok else 'NO':>7}"
        )
    eligible = sum(m.eligible for m in analysis.markets)
    lines.append(f"\n{eligible}/{len(analysis.markets)} markets pass both locked filters")
    for m in analysis.markets:
        if not m.eligible:
            lines.append(f"  not eligible: {m.snapshot.venue_name}: {', '.join(m.reasons)}")
    unknown = [m.snapshot.venue_name for m in analysis.markets if not m.venue_type_known]
    if unknown:
        lines.append(f"\nvenue type UNKNOWN (set ZERO_DELTA_VENUE_TYPES): {', '.join(sorted(unknown))}")
    provisional = [m.snapshot.venue_name for m in analysis.markets if m.venue_type_known and not m.venue_type_verified]
    if provisional:
        lines.append("* venue type is a PROVISIONAL built-in assumption; confirm via ZERO_DELTA_VENUE_TYPES")
    for i in analysis.issues:
        lines.append(f"issue {i.code}: {i.venue or '-'}: {i.message}")
    return "\n".join(lines)


async def _run(asset: str, offline: bool) -> AssetAnalysis:
    provider = ZeroDeltaProvider(ZeroDeltaSettings())
    try:
        if offline:
            payload = json.loads((FIXTURES / f"{asset.lower()}_dash_response.json").read_text("utf-8"))
            return provider.analyze_response(payload, asset)
        return await provider.fetch_asset(asset)
    finally:
        await provider.aclose()


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("asset", help="BTC, ETH or SOL")
    ap.add_argument("--offline", action="store_true", help="use the saved fixture instead of Zero Delta")
    args = ap.parse_args()
    try:
        print(render(asyncio.run(_run(args.asset, args.offline))))
    except FileNotFoundError:
        sys.exit(f"No fixture for {args.asset}. Available: BTC, ETH, SOL")
    except ZeroDeltaError as exc:
        sys.exit(f"Zero Delta error: {exc}")


if __name__ == "__main__":
    main()
