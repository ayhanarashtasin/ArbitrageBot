"""Read contract structure from Zero Delta's market identifier (ccxt style: BASE/QUOTE:SETTLE).

Only what the identifier itself proves is concluded. Contract multiplier, quantity units, tick and
lot sizes are NOT in this data and come from direct venue metadata (Phase 3).
"""

from dataclasses import dataclass
from enum import StrEnum


class ContractKind(StrEnum):
    LINEAR = "LINEAR"  # settled in the quote currency
    INVERSE = "INVERSE"  # settled in the base asset
    UNKNOWN = "UNKNOWN"


@dataclass(frozen=True)
class Instrument:
    base: str
    quote: str
    settle: str
    kind: ContractKind


def parse_market_identifier(identifier: str) -> Instrument | None:
    """'BTC/USDC:USDC' -> Instrument(BTC, USDC, USDC, LINEAR). None when the shape is not recognised."""
    pair, sep, settle = identifier.partition(":")
    base, slash, quote = pair.partition("/")
    if not (sep and slash and base and quote and settle):
        return None
    base, quote, settle = base.upper(), quote.upper(), settle.upper()
    if settle == base:
        kind = ContractKind.INVERSE
    elif settle == quote:
        kind = ContractKind.LINEAR
    else:
        kind = ContractKind.UNKNOWN
    return Instrument(base, quote, settle, kind)
