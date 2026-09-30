"""Step 2 of the plan: choose the best pairs and say which exchanges they are on.

The selection rule is deliberately plain and visible: rank by ONE metric, the 7-day average funding edge (or the
median, by setting), and take the top N. project.md forbids inventing a weighted score, so nothing else is blended in.
Steadiness, sign flips and volume are shown next to each pick so a human can judge it, and every trade still has to
pass the live safety checks before any (paper) order.

Two lists are produced from the same ranking:
  best in the market   the top N of ALL pairs, tradable or not, so it is always clear what the best opportunities are
  best we can trade    the top N of the pairs both of whose venues have an adapter and are different exchanges
Only the second list can lead to an order.

Filters (not scores) applied before taking the top N, each switchable:
  one_per_asset       one pair per coin, so three picks are three different opportunities (not one short leg paired
                      with three interchangeable long legs)
  require_live_edge   the pair's CURRENT funding edge must still be positive: a 7-day average whose edge has already
                      gone cannot be entered (the spread rule would refuse it)
  blocked             a reason per pair that stops it being traded now (the stock earnings blackout); such a pair can
                      still appear in the market list, marked, but never in the tradable list
"""

from collections.abc import Callable
from dataclasses import dataclass
from decimal import Decimal

from app.strategy.pairs import PairAnalytics

APR = Decimal(8760)


@dataclass(frozen=True)
class Pick:
    rank: int
    pair: PairAnalytics
    score_apr: Decimal  # the ranking metric, as annualised percent
    tradable: bool
    why_not: str | None  # plain reason when it cannot be traded


def score(pair: PairAnalytics, metric: str) -> Decimal:
    """Annualised percent. The pair's edge is percent per hour, so x 8760."""
    stats = pair.stats
    return (stats.median if metric == "median" else stats.mean) * APR


def rank(pairs: list[PairAnalytics], metric: str = "mean") -> list[PairAnalytics]:
    """Best first. Only complete, positive-edge pairs take part; ties break on steadiness, then names (stable)."""
    usable = [p for p in pairs if p.status == "OK" and p.stats is not None and score(p, metric) > 0]
    return sorted(usable, key=lambda p: (-score(p, metric), -p.stats.direction_consistency_ratio,
                                         p.long_leg.venue_name, p.short_leg.venue_name, p.asset))


def why_not_tradable(pair: PairAnalytics, source_for_venue: Callable[[str], object | None]) -> str | None:
    long_src, short_src = source_for_venue(pair.long_leg.venue_name), source_for_venue(pair.short_leg.venue_name)
    missing = [n for n, s in ((pair.long_leg.venue_name, long_src), (pair.short_leg.venue_name, short_src)) if s is None]
    if missing:
        return "no adapter yet for " + " and ".join(missing)
    if long_src is short_src:
        return "both legs are on the same exchange"
    return None


def _top(flagged: list[tuple[PairAnalytics, str | None]], count: int, one_per_asset: bool
         ) -> list[tuple[PairAnalytics, str | None]]:
    out, seen = [], set()
    for p, why in flagged:
        if one_per_asset and p.asset in seen:
            continue
        seen.add(p.asset)
        out.append((p, why))
        if len(out) == count:
            break
    return out


def select(pairs: list[PairAnalytics], count: int, metric: str,
           source_for_venue: Callable[[str], object | None], *, one_per_asset: bool = False,
           require_live_edge: bool = False,
           blocked: Callable[[PairAnalytics], str | None] | None = None) -> tuple[list[Pick], list[Pick]]:
    """(best in the market, best we can trade), each at most `count` long, ranked from 1."""
    ranked = rank(pairs, metric)
    if require_live_edge:
        ranked = [p for p in ranked if p.current_funding_edge > 0]
    flagged = []
    for p in ranked:
        why = why_not_tradable(p, source_for_venue)
        if why is None and blocked is not None:
            why = blocked(p)
        flagged.append((p, why))
    overall = [Pick(i + 1, p, score(p, metric), why is None, why)
               for i, (p, why) in enumerate(_top(flagged, count, one_per_asset))]
    tradable = [Pick(i + 1, p, score(p, metric), True, None)
                for i, (p, _) in enumerate(_top([f for f in flagged if f[1] is None], count, one_per_asset))]
    return overall, tradable
