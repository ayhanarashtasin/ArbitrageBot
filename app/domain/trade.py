"""Trade lifecycle state machine (project.md section 63). Every transition is validated and persisted."""

from enum import StrEnum


class TradeState(StrEnum):
    DISCOVERED = "DISCOVERED"
    VALIDATING_HISTORY = "VALIDATING_HISTORY"
    DIRECT_VERIFYING = "DIRECT_VERIFYING"
    WAITING_FOR_LOW_VOL = "WAITING_FOR_LOW_VOL"
    ENTRY_READY = "ENTRY_READY"
    ENTERING = "ENTERING"
    HEDGED_OPEN = "HEDGED_OPEN"
    EXIT_PENDING = "EXIT_PENDING"
    CONTROLLED_EXIT = "CONTROLLED_EXIT"
    EMERGENCY_EXIT = "EMERGENCY_EXIT"
    EXITING = "EXITING"
    CLOSED = "CLOSED"
    # dead ends before any order
    DATA_INSUFFICIENT = "DATA_INSUFFICIENT"
    EXCHANGE_MISMATCH = "EXCHANGE_MISMATCH"
    REJECTED = "REJECTED"
    # failure states
    DEGRADED = "DEGRADED"
    RECONCILING = "RECONCILING"
    ERROR = "ERROR"
    MANUAL_INTERVENTION_REQUIRED = "MANUAL_INTERVENTION_REQUIRED"


S = TradeState

TERMINAL = frozenset({S.CLOSED, S.DATA_INSUFFICIENT, S.EXCHANGE_MISMATCH, S.REJECTED})
FAILURE = frozenset({S.DEGRADED, S.RECONCILING, S.ERROR, S.MANUAL_INTERVENTION_REQUIRED})
# States in which the trade may hold exposure and therefore needs risk management.
EXPOSED = frozenset({S.ENTERING, S.HEDGED_OPEN, S.EXIT_PENDING, S.CONTROLLED_EXIT, S.EMERGENCY_EXIT,
                     S.EXITING, S.DEGRADED, S.RECONCILING, S.ERROR, S.MANUAL_INTERVENTION_REQUIRED})

_FORWARD: dict[TradeState, frozenset[TradeState]] = {
    S.DISCOVERED: frozenset({S.VALIDATING_HISTORY, S.REJECTED}),
    S.VALIDATING_HISTORY: frozenset({S.DIRECT_VERIFYING, S.DATA_INSUFFICIENT, S.REJECTED}),
    S.DIRECT_VERIFYING: frozenset({S.WAITING_FOR_LOW_VOL, S.ENTRY_READY, S.EXCHANGE_MISMATCH, S.REJECTED}),
    S.WAITING_FOR_LOW_VOL: frozenset({S.ENTRY_READY, S.REJECTED, S.DIRECT_VERIFYING}),
    S.ENTRY_READY: frozenset({S.ENTERING, S.REJECTED, S.WAITING_FOR_LOW_VOL, S.DIRECT_VERIFYING}),
    S.ENTERING: frozenset({S.HEDGED_OPEN, S.CONTROLLED_EXIT, S.EMERGENCY_EXIT, S.EXITING}),
    S.HEDGED_OPEN: frozenset({S.EXIT_PENDING, S.CONTROLLED_EXIT, S.EMERGENCY_EXIT}),
    S.EXIT_PENDING: frozenset({S.EXITING, S.CONTROLLED_EXIT, S.EMERGENCY_EXIT}),
    S.CONTROLLED_EXIT: frozenset({S.EXITING, S.EMERGENCY_EXIT}),
    S.EMERGENCY_EXIT: frozenset({S.EXITING}),
    S.EXITING: frozenset({S.CLOSED, S.EMERGENCY_EXIT}),
}

# Any exposed state may fall into a failure state, and a failure state may return to any exposed state
# once reconciliation shows the venues and the database agree.
_FAILURE_ENTRY = EXPOSED - {S.DEGRADED, S.RECONCILING, S.ERROR, S.MANUAL_INTERVENTION_REQUIRED}


class InvalidTransition(ValueError):
    pass


def allowed_next(state: TradeState) -> frozenset[TradeState]:
    if state in TERMINAL:
        return frozenset()
    nxt = set(_FORWARD.get(state, frozenset()))
    if state in _FAILURE_ENTRY:
        nxt |= {S.DEGRADED, S.RECONCILING, S.ERROR, S.MANUAL_INTERVENTION_REQUIRED}
    if state in (S.DEGRADED, S.RECONCILING):
        nxt |= {S.RECONCILING, S.ERROR, S.MANUAL_INTERVENTION_REQUIRED,
                S.HEDGED_OPEN, S.ENTERING, S.EXITING, S.EMERGENCY_EXIT, S.CONTROLLED_EXIT, S.CLOSED}
        nxt.discard(state)
    if state is S.ERROR:
        nxt |= {S.RECONCILING, S.MANUAL_INTERVENTION_REQUIRED, S.EMERGENCY_EXIT}
    if state is S.MANUAL_INTERVENTION_REQUIRED:
        nxt |= {S.RECONCILING, S.EMERGENCY_EXIT, S.EXITING, S.CLOSED}
    return frozenset(nxt)


def check_transition(current: TradeState, new: TradeState) -> None:
    if new not in allowed_next(current):
        raise InvalidTransition(f"{current} -> {new} is not allowed")
