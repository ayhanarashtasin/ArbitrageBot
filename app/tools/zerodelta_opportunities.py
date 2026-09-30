"""Candidate pairs with seven-day funding-edge analytics for one asset.

    python -m app.tools.zerodelta_opportunities BTC [--offline] [--top 15] [--sort mean|current|stddev|consistency]

The sort only orders the printout. project.md defines no ranking formula, so nothing here is a score.
Edges are percent per hour; the table also shows the annualized equivalent (x 8760) for readability.
"""

import argparse
import asyncio
import json
import sys
from decimal import Decimal

from app.core.config import ZeroDeltaSettings
from app.providers.zerodelta import ZeroDeltaError, ZeroDeltaProvider
from app.strategy.pairs import PairAnalytics, generate_pairs
from app.tools.zerodelta_check import FIXTURES

APR = Decimal(8760)
SORTS = {
    "mean": lambda p: -p.stats.mean,
    "current": lambda p: -p.current_funding_edge,
    "stddev": lambda p: p.stats.stddev,
    "consistency": lambda p: -p.stats.direction_consistency_ratio,
}


def render(pairs: list[PairAnalytics], top: int, sort: str) -> str:
    ok = [p for p in pairs if p.status == "OK"]
    bad = [p for p in pairs if p.status != "OK"]
    ok.sort(key=SORTS[sort])
    head = (
        f"{'long':22} {'short':22} {'types':17} {'now':>7} {'mean':>7} {'median':>7} {'std':>6} "
        f"{'pos%':>5} {'dir%':>5} {'flips':>5} {'streak':>6} {'hours':>5} {'obs':>4} {'vol $M min':>10}"
    )
    lines = [f"{len(ok)} pairs OK, {len(bad)} DATA_INSUFFICIENT. Edge in APR %, display order: {sort}", head, "-" * len(head)]
    for p in ok[:top]:
        st = p.stats
        lines.append(
            f"{p.long_leg.venue_name[:22]:22} {p.short_leg.venue_name[:22]:22} {p.pair_type:17} "
            f"{p.current_funding_edge * APR:7.1f} {st.mean * APR:7.1f} {st.median * APR:7.1f} "
            f"{st.stddev * APR:6.1f} {st.positive_edge_ratio * 100:5.0f} "
            f"{st.direction_consistency_ratio * 100:5.0f} {st.sign_flip_count:5d} "
            f"{st.longest_positive_edge_streak:6d} {p.coverage.coverage_hours:5.0f} "
            f"{p.coverage.observation_count:4d} "
            f"{min(p.long_leg.avg_24h_volume_usd, p.short_leg.avg_24h_volume_usd) / 1_000_000:10.1f}"
        )
    unverified = sum(1 for p in ok if not (p.long_leg.venue_type_verified and p.short_leg.venue_type_verified))
    lines.append(
        f"\n{unverified}/{len(ok)} pairs involve a venue type that is not user-verified. "
        "Contract multipliers are unverified (Phase 3). Basis and provider score are not available."
    )
    mismatch = sum(1 for p in ok if p.settlement_mismatch)
    if mismatch:
        lines.append(f"{mismatch} pairs settle in different assets (settlement_mismatch): collateral check needed.")
    return "\n".join(lines)


async def _pairs(asset: str, offline: bool) -> list[PairAnalytics]:
    settings = ZeroDeltaSettings()
    provider = ZeroDeltaProvider(settings)
    try:
        if offline:
            payload = json.loads((FIXTURES / f"{asset.lower()}_dash_response.json").read_text("utf-8"))
            return generate_pairs(provider.analyze_response(payload, asset), settings)
        return await provider.get_candidate_pairs(asset)
    finally:
        await provider.aclose()


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("asset", help="BTC, ETH or SOL")
    ap.add_argument("--offline", action="store_true")
    ap.add_argument("--top", type=int, default=15)
    ap.add_argument("--sort", choices=SORTS, default="mean")
    args = ap.parse_args()
    try:
        print(render(asyncio.run(_pairs(args.asset, args.offline)), args.top, args.sort))
    except FileNotFoundError:
        sys.exit(f"No fixture for {args.asset}. Available: BTC, ETH, SOL")
    except ZeroDeltaError as exc:
        sys.exit(f"Zero Delta error: {exc}")


if __name__ == "__main__":
    main()
