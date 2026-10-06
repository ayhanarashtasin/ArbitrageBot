"""Seven-day funding-edge statistics for one ordered pair (project.md section 14).

Units: percent per hour (normalized funding, section 12). All arithmetic is Decimal.
Sign convention: positive funding = LONG pays SHORT, so the edge of a hedge is
    edge = short_funding_per_hour - long_funding_per_hour        (section 19)
Standard deviation is the population value over the complete 7-day window.
"""

import statistics
from decimal import Decimal

from pydantic import BaseModel, ConfigDict


class EdgeStats(BaseModel):
    model_config = ConfigDict(frozen=True)

    mean: Decimal
    median: Decimal
    minimum: Decimal
    maximum: Decimal
    stddev: Decimal
    positive_edge_ratio: Decimal  # share of hours with edge > 0
    direction_consistency_ratio: Decimal  # share of hours in the dominant direction
    sign_flip_count: int
    longest_positive_edge_streak: int  # consecutive hourly observations with edge > 0
    latest: Decimal  # last observation in the window
    largest_positive_contribution: Decimal | None = None
    top_three_positive_contribution: Decimal | None = None
    mean_without_largest: Decimal | None = None


def _sign(x: Decimal) -> int:
    return (x > 0) - (x < 0)


def compute_edge_stats(edges: list[Decimal]) -> EdgeStats:
    """`edges` are hourly edge values in time order. Needs at least one value."""
    if not edges:
        raise ValueError("no edge values")
    n = len(edges)
    total = sum(edges, Decimal(0))
    positives = sorted((e for e in edges if e > 0), reverse=True)
    positive_total = sum(positives, Decimal(0))
    positive = sum(1 for e in edges if e > 0)
    negative = sum(1 for e in edges if e < 0)
    signs = [_sign(e) for e in edges if e != 0]
    flips = sum(1 for a, b in zip(signs, signs[1:]) if a != b)
    best = run = 0
    for e in edges:
        run = run + 1 if e > 0 else 0
        best = max(best, run)
    return EdgeStats(
        mean=statistics.mean(edges),
        median=statistics.median(edges),
        minimum=min(edges),
        maximum=max(edges),
        stddev=statistics.pstdev(edges),
        positive_edge_ratio=Decimal(positive) / n,
        direction_consistency_ratio=Decimal(max(positive, negative)) / n,
        sign_flip_count=flips,
        longest_positive_edge_streak=best,
        latest=edges[-1],
        largest_positive_contribution=positives[0] / positive_total if positive_total else None,
        top_three_positive_contribution=sum(positives[:3], Decimal(0)) / positive_total if positive_total else None,
        mean_without_largest=(total - max(edges)) / (n - 1) if n > 1 else None,
    )
