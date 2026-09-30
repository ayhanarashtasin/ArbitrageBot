"""Database access. One short session per call keeps the engine free of ORM details.

Every state transition of a trade is validated against the state machine (section 63) and written to the
audit log in the same transaction, so history can always be reconstructed (section 43).
"""

import functools
import json
import logging
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any

from sqlalchemy import create_engine, func, select, update
from sqlalchemy.exc import InterfaceError, OperationalError
from sqlalchemy.exc import TimeoutError as PoolTimeoutError
from sqlalchemy.orm import Session, sessionmaker

from app.core.clock import Clock
from app.domain.trade import TERMINAL, InvalidTransition, TradeState, check_transition
from app.persistence import models as m


log = logging.getLogger(__name__)

# Connection-level failures only. Integrity errors are bugs and must never be swallowed.
DB_OUTAGE_ERRORS = (OperationalError, InterfaceError, PoolTimeoutError)
MAX_BACKLOG = 20_000


def _iso_utc(dt) -> str:
    from app.persistence.models import as_utc
    return as_utc(dt).isoformat()


def _json_safe(value: Any) -> Any:
    """Round-trip through JSON so Decimals, datetimes and enums become plain strings."""
    return json.loads(json.dumps(value, default=str))


class Repository:
    def __init__(self, url: str, clock: Clock):
        if url.startswith("sqlite:///") and ":memory:" not in url:
            Path(url.removeprefix("sqlite:///")).parent.mkdir(parents=True, exist_ok=True)
        kwargs = {}
        if url in ("sqlite://", "sqlite:///:memory:"):
            from sqlalchemy.pool import StaticPool
            kwargs = {"poolclass": StaticPool, "connect_args": {"check_same_thread": False}}
        self.engine = create_engine(url, **kwargs)
        m.Base.metadata.create_all(self.engine)
        self._sessions = sessionmaker(self.engine, expire_on_commit=False)
        self.clock = clock
        # Database outage handling: writes are buffered in order and replayed when the database returns,
        # so risk management and exits never depend on the database being up (project.md section 68).
        self.db_ok = True
        self._backlog: list[tuple] = []
        self._flushing = False
        self._kv_cache: dict[str, dict] = {}

    @contextmanager
    def session(self) -> Iterator[Session]:
        s = self._sessions()
        try:
            yield s
            s.commit()
        except Exception:
            s.rollback()
            raise
        finally:
            s.close()

    # ---- generic -------------------------------------------------------------------------------
    def add(self, model, **fields) -> Any:
        with self.session() as s:
            row = model(**fields)
            s.add(row)
            s.flush()
            return row

    def audit(self, kind: str, data: dict, trade_id: str | None = None) -> None:
        self.add(m.AuditLog, trade_id=trade_id, kind=kind, data=_json_safe(data), recorded_at=self.clock.now())

    def get_kv(self, key: str) -> dict | None:
        with self.session() as s:
            row = s.get(m.KeyValue, key)
            return None if row is None else dict(row.value)

    def set_kv(self, key: str, value: dict) -> None:
        with self.session() as s:
            row = s.get(m.KeyValue, key)
            if row is None:
                s.add(m.KeyValue(key=key, value=_json_safe(value), updated_at=self.clock.now()))
            else:
                row.value, row.updated_at = _json_safe(value), self.clock.now()

    # ---- decisions and alerts ------------------------------------------------------------------
    def add_decision(self, *, asset: str, long_venue: str, short_venue: str, outcome: str, checks: list[dict],
                     metrics: dict, trade_id: str | None = None) -> None:
        self.add(m.StrategyDecision, asset=asset, long_venue=long_venue, short_venue=short_venue, outcome=outcome,
                 checks=_json_safe(checks), metrics=_json_safe(metrics), trade_id=trade_id,
                 recorded_at=self.clock.now())

    def add_alert(self, kind: str, severity: str, message: str, trade_id: str | None = None,
                  delivered: bool = False) -> int:
        return self.add(m.Alert, kind=kind, severity=severity, message=message, trade_id=trade_id,
                        delivered=delivered, recorded_at=self.clock.now()).id

    def mark_alert_delivered(self, alert_id: int) -> None:
        with self.session() as s:
            s.execute(update(m.Alert).where(m.Alert.id == alert_id).values(delivered=True))

    def add_risk_event(self, trade_id: str | None, action: str, priority: int, reasons) -> None:
        self.add(m.RiskEvent, trade_id=trade_id, action=action, priority=priority, reasons=list(reasons),
                 recorded_at=self.clock.now())

    def add_provider_event(self, kind: str, message: str) -> None:
        self.add(m.ZeroDeltaProviderEvent, kind=kind, message=message, recorded_at=self.clock.now())

    # ---- trades --------------------------------------------------------------------------------
    def next_trade_id(self) -> str:
        with self.session() as s:
            n = s.scalar(select(func.count()).select_from(m.Trade)) or 0
        return f"arb{n + 1:04d}"

    def create_trade(self, **fields) -> m.Trade:
        fields.setdefault("context", {})
        fields["context"] = _json_safe(fields["context"])
        trade = self.add(m.Trade, **fields)
        self.audit("trade_created", {"state": fields["state"]}, trade_id=fields["id"])
        return trade

    def get_trade(self, trade_id: str) -> m.Trade:
        with self.session() as s:
            row = s.get(m.Trade, trade_id)
            if row is None:
                raise KeyError(trade_id)
            return row

    def update_trade(self, trade_id: str, **fields) -> None:
        if "context" in fields:
            fields["context"] = _json_safe(fields["context"])
        with self.session() as s:
            s.execute(update(m.Trade).where(m.Trade.id == trade_id).values(**fields))

    def merge_context(self, trade_id: str, **items) -> None:
        with self.session() as s:
            row = s.get(m.Trade, trade_id)
            ctx = dict(row.context or {})
            ctx.update(_json_safe(items))
            row.context = ctx

    def set_state(self, trade_id: str, new: TradeState, reason: str = "") -> TradeState:
        """Validate and persist a transition. Raises InvalidTransition without changing anything."""
        with self.session() as s:
            row = s.get(m.Trade, trade_id)
            current = TradeState(row.state)
            if current is new:
                return current
            check_transition(current, new)  # raises InvalidTransition
            row.state = new.value
            if new in TERMINAL:
                row.closed_at = self.clock.now()
            s.add(m.AuditLog(trade_id=trade_id, kind="state_transition", recorded_at=self.clock.now(),
                             data={"from": current.value, "to": new.value, "reason": reason}))
            return current

    def unfinished_trades(self) -> list[m.Trade]:
        with self.session() as s:
            rows = s.scalars(select(m.Trade).where(m.Trade.state.notin_([t.value for t in TERMINAL]))).all()
            return list(rows)

    def all_trades(self) -> list[m.Trade]:
        with self.session() as s:
            return list(s.scalars(select(m.Trade).order_by(m.Trade.opened_at)).all())

    # ---- legs, slices, orders, fills -----------------------------------------------------------
    def upsert_leg(self, trade_id: str, role: str, venue: str, symbol: str, **totals) -> None:
        with self.session() as s:
            row = s.scalar(select(m.TradeLeg).where(m.TradeLeg.trade_id == trade_id, m.TradeLeg.role == role))
            if row is None:
                s.add(m.TradeLeg(trade_id=trade_id, role=role, venue=venue, symbol=symbol, **totals))
            else:
                for k, v in totals.items():
                    setattr(row, k, v)

    def record_slice(self, trade_id: str, phase: str, index: int, planned: Decimal, status: str, **fields) -> None:
        with self.session() as s:
            row = s.scalar(select(m.TwapSlice).where(
                m.TwapSlice.trade_id == trade_id, m.TwapSlice.phase == phase, m.TwapSlice.slice_index == index))
            fields = {k: (_json_safe(v) if k == "detail" else v) for k, v in fields.items()}
            if row is None:
                s.add(m.TwapSlice(trade_id=trade_id, phase=phase, slice_index=index, planned_notional=planned,
                                  status=status, **fields))
            else:
                row.status = status
                for k, v in fields.items():
                    setattr(row, k, v)

    def slices(self, trade_id: str, phase: str) -> list[m.TwapSlice]:
        with self.session() as s:
            return list(s.scalars(select(m.TwapSlice).where(
                m.TwapSlice.trade_id == trade_id, m.TwapSlice.phase == phase).order_by(m.TwapSlice.slice_index)))

    def upsert_order(self, trade_id: str, leg: str, order, reduce_only: bool) -> None:
        """`order` is an OrderState from an adapter."""
        with self.session() as s:
            row = s.get(m.Order, order.client_order_id)
            values = dict(trade_id=trade_id, venue=order.venue, symbol=order.symbol, leg=leg,
                          side=order.side.value, requested_quantity=order.requested_quantity,
                          filled_quantity=order.filled_quantity, average_fill_price=order.average_fill_price,
                          fee_usd=order.fee_paid_usd, status=order.status.value, reduce_only=reduce_only,
                          updated_at=self.clock.now())
            if row is None:
                s.add(m.Order(client_order_id=order.client_order_id, **values))
            else:
                for k, v in values.items():
                    setattr(row, k, v)

    def add_fill(self, trade_id: str, cid: str, quantity: Decimal, price: Decimal, fee: Decimal,
                 reference_price: Decimal | None, when: datetime) -> None:
        with self.session() as s:
            exists = s.scalar(select(m.Fill.id).where(m.Fill.client_order_id == cid, m.Fill.quantity == quantity,
                                                      m.Fill.filled_at == when))
            if exists is None:
                s.add(m.Fill(client_order_id=cid, trade_id=trade_id, quantity=quantity, price=price, fee_usd=fee,
                             reference_price=reference_price, filled_at=when))

    def orders_for(self, trade_id: str) -> list[m.Order]:
        with self.session() as s:
            return list(s.scalars(select(m.Order).where(m.Order.trade_id == trade_id)))

    def order_exists(self, client_order_id: str) -> bool:
        with self.session() as s:
            return s.get(m.Order, client_order_id) is not None

    # ---- snapshots -----------------------------------------------------------------------------
    def add_equity_snapshot(self, trade_id: str, **fields) -> None:
        self.add(m.EquitySnapshot, trade_id=trade_id, recorded_at=self.clock.now(), **fields)

    def add_position_snapshot(self, trade_id: str, leg: str, quantity: Decimal, mark: Decimal | None,
                              exposure: Decimal) -> None:
        self.add(m.PositionSnapshot, trade_id=trade_id, leg=leg, quantity=quantity, mark_price=mark,
                 exposure_usd=exposure, recorded_at=self.clock.now())

    def add_funding_payment(self, trade_id: str, venue: str, symbol: str, when: datetime, rate: Decimal,
                            notional: Decimal, expected: Decimal, actual: Decimal | None) -> None:
        self.add(m.FundingPayment, trade_id=trade_id, venue=venue, symbol=symbol, timestamp=when, rate=rate,
                 notional=notional, expected_payment=expected, actual_payment=actual,
                 difference=None if actual is None else actual - expected)

    def funding_payments(self, trade_id: str) -> list[m.FundingPayment]:
        with self.session() as s:
            return list(s.scalars(select(m.FundingPayment).where(m.FundingPayment.trade_id == trade_id)))

    # ---- paper accounts ------------------------------------------------------------------------
    def save_paper_account(self, key: str, state: dict) -> None:
        with self.session() as s:
            row = s.get(m.PaperAccount, key)
            if row is None:
                s.add(m.PaperAccount(venue_key=key, state=_json_safe(state), updated_at=self.clock.now()))
            else:
                row.state, row.updated_at = _json_safe(state), self.clock.now()

    def load_paper_account(self, key: str) -> dict | None:
        with self.session() as s:
            row = s.get(m.PaperAccount, key)
            return None if row is None else dict(row.state)

    # ---- execution ownership (section 52) ------------------------------------------------------
    def acquire_lease(self, trade_id: str, owner: str, ttl_seconds: int = 60) -> bool:
        """Compare-and-set: succeeds if unowned, already ours, or the previous lease expired."""
        now = self.clock.now()
        with self.session() as s:
            result = s.execute(
                update(m.Trade).where(
                    m.Trade.id == trade_id,
                    (m.Trade.owner.is_(None)) | (m.Trade.owner == owner) | (m.Trade.lease_expires_at < now),
                ).values(owner=owner, lease_expires_at=now + timedelta(seconds=ttl_seconds)))
            return result.rowcount == 1

    def release_lease(self, trade_id: str, owner: str) -> None:
        with self.session() as s:
            s.execute(update(m.Trade).where(m.Trade.id == trade_id, m.Trade.owner == owner)
                      .values(owner=None, lease_expires_at=None))

    # ---- audit reads ---------------------------------------------------------------------------
    def audit_for(self, trade_id: str) -> list[m.AuditLog]:
        with self.session() as s:
            return list(s.scalars(select(m.AuditLog).where(m.AuditLog.trade_id == trade_id).order_by(m.AuditLog.id)))

    def alerts(self, kind: str | None = None) -> list[m.Alert]:
        with self.session() as s:
            q = select(m.Alert).order_by(m.Alert.id)
            if kind:
                q = q.where(m.Alert.kind == kind)
            return list(s.scalars(q))

    def risk_events(self, trade_id: str) -> list[m.RiskEvent]:
        with self.session() as s:
            return list(s.scalars(select(m.RiskEvent).where(m.RiskEvent.trade_id == trade_id).order_by(m.RiskEvent.id)))


    # ---- reads for the dashboard --------------------------------------------------------------
    def recent_decisions(self, limit: int = 60) -> list[m.StrategyDecision]:
        with self.session() as s:
            return list(s.scalars(select(m.StrategyDecision).order_by(m.StrategyDecision.id.desc()).limit(limit)))

    def recent_alerts(self, limit: int = 40) -> list[m.Alert]:
        with self.session() as s:
            return list(s.scalars(select(m.Alert).order_by(m.Alert.id.desc()).limit(limit)))

    def recent_risk_events(self, limit: int = 30) -> list[m.RiskEvent]:
        with self.session() as s:
            return list(s.scalars(select(m.RiskEvent).order_by(m.RiskEvent.id.desc()).limit(limit)))

    def equity_series(self, trade_id: str, limit: int = 400) -> list[m.EquitySnapshot]:
        with self.session() as s:
            rows = list(s.scalars(select(m.EquitySnapshot).where(m.EquitySnapshot.trade_id == trade_id)
                                  .order_by(m.EquitySnapshot.id.desc()).limit(limit)))
        return rows[::-1]

    def prune_discovery(self, keep_hours: int = 72) -> None:
        """Drop old per-scan discovery rows. Trades, decisions and selections are never pruned."""
        from datetime import timedelta
        from sqlalchemy import delete
        cutoff = self.clock.now() - timedelta(hours=keep_hours)
        with self.session() as s:
            s.execute(delete(m.ZeroDeltaMarket).where(m.ZeroDeltaMarket.received_at < cutoff))
            s.execute(delete(m.ZeroDeltaHistory).where(m.ZeroDeltaHistory.recorded_at < cutoff))
            s.execute(delete(m.ZeroDeltaProviderEvent).where(m.ZeroDeltaProviderEvent.recorded_at < cutoff))

    def latest_selection(self) -> dict:
        """The most recent saved selection, rebuilt from the database (empty dict if none)."""
        saved = self.get_kv("last_selection")
        if saved:
            return {**saved, "from_database": True}
        with self.session() as s:
            last = s.scalar(select(func.max(m.Selection.run_at)))
            if last is None:
                return {}
            rows = list(s.scalars(select(m.Selection).where(m.Selection.run_at == last)
                                  .order_by(m.Selection.kind, m.Selection.rank)))
        out: dict = {"at": _iso_utc(last), "overall": [], "tradable": [], "from_database": True}
        for r in rows:
            out[r.kind].append(r.detail)
        out["count"] = max(len(out["overall"]), len(out["tradable"]))
        return out

    def due_selections(self, limit: int = 200) -> list[m.Selection]:
        cutoff = self.clock.now() - timedelta(days=7, hours=1)
        with self.session() as s:
            # SQL JSON indexing works on both SQLite and PostgreSQL; old rows without frozen pairs cannot be scored.
            q = select(m.Selection).where(m.Selection.run_at <= cutoff,
                m.Selection.detail["pair"].as_string().is_not(None),
                m.Selection.detail["forward_score"].as_string().is_(None)).order_by(m.Selection.id).limit(limit)
            return list(s.scalars(q))

    def score_selection(self, selection_id: int, result: dict) -> None:
        with self.session() as s:
            row = s.get(m.Selection, selection_id)
            row.detail = {**row.detail, "forward_score": _json_safe(result)}

    def selection_track_record(self, limit: int = 60) -> list[dict]:
        with self.session() as s:
            rows = s.scalars(select(m.Selection).where(
                m.Selection.detail["forward_score"].as_string().is_not(None))
                .order_by(m.Selection.id.desc()).limit(limit)).all()
            return [{"id": r.id, "picked_at": _iso_utc(r.run_at), "kind": r.kind, "asset": r.asset,
                     "long": r.long_venue, "short": r.short_venue, "score_apr": float(r.score_apr),
                     **r.detail["forward_score"]} for r in rows]

    def all_funding_payments(self) -> list[m.FundingPayment]:
        with self.session() as s:
            return list(s.scalars(select(m.FundingPayment).order_by(m.FundingPayment.timestamp)))

    def trade_legs(self, trade_id: str) -> list[m.TradeLeg]:
        with self.session() as s:
            return list(s.scalars(select(m.TradeLeg).where(m.TradeLeg.trade_id == trade_id)))

    def all_slices(self, trade_id: str) -> list[m.TwapSlice]:
        with self.session() as s:
            return list(s.scalars(select(m.TwapSlice).where(m.TwapSlice.trade_id == trade_id)
                                  .order_by(m.TwapSlice.phase, m.TwapSlice.slice_index)))

    # ---- outage handling ----------------------------------------------------------------------
    @property
    def backlog_size(self) -> int:
        return len(self._backlog)

    def flush_backlog(self) -> bool:
        """Replay buffered writes in order. Returns True when nothing is left."""
        if self._flushing or not self._backlog:
            return not self._backlog
        self._flushing = True
        try:
            while self._backlog:
                fn, args, kwargs = self._backlog[0]
                try:
                    fn(self, *args, **kwargs)
                except DB_OUTAGE_ERRORS:
                    self.db_ok = False
                    return False
                except Exception:  # noqa: BLE001 - a write that no longer applies must not wedge the replay
                    log.exception("dropping a buffered write that failed on replay: %s", getattr(fn, "__name__", fn))
                self._backlog.pop(0)
            self.db_ok = True
            return True
        finally:
            self._flushing = False


def _resilient(fn, default=None, *, buffer=True):
    """Wrap a write method: on a connection failure buffer it and return `default` instead of raising."""

    @functools.wraps(fn)
    def inner(self: Repository, *args, **kwargs):
        if self._backlog and not self._flushing:
            self.flush_backlog()
        try:
            result = fn(self, *args, **kwargs)
            if not self._backlog:
                self.db_ok = True
            return result
        except DB_OUTAGE_ERRORS as exc:
            if self.db_ok:
                log.error("database unavailable (%s): buffering writes, trading continues", type(exc).__name__)
            self.db_ok = False
            if buffer and len(self._backlog) < MAX_BACKLOG:
                self._backlog.append((fn, args, kwargs))
            return default() if callable(default) else default

    return inner


_ORIGINAL_SET_KV = Repository.set_kv
_ORIGINAL_GET_KV = Repository.get_kv


def _set_kv(self: Repository, key: str, value: dict) -> None:
    self._kv_cache[key] = _json_safe(value)
    _ORIGINAL_SET_KV(self, key, value)


def _get_kv(self: Repository, key: str) -> dict | None:
    try:
        value = _ORIGINAL_GET_KV(self, key)
        if value is not None:
            self._kv_cache[key] = value
        return value
    except DB_OUTAGE_ERRORS:
        self.db_ok = False
        return self._kv_cache.get(key)


Repository.set_kv = _resilient(_set_kv)  # type: ignore[method-assign]
Repository.get_kv = _get_kv  # type: ignore[method-assign]
for _name in ("audit", "add_decision", "mark_alert_delivered", "add_risk_event", "add_provider_event", "update_trade",
              "merge_context", "upsert_leg", "record_slice", "upsert_order", "add_fill", "add_equity_snapshot",
              "add_position_snapshot", "add_funding_payment", "save_paper_account", "release_lease", "set_state"):
    setattr(Repository, _name, _resilient(getattr(Repository, _name)))
Repository.add_alert = _resilient(Repository.add_alert, default=-1)  # type: ignore[method-assign]
Repository.acquire_lease = _resilient(Repository.acquire_lease, default=True, buffer=False)  # type: ignore[method-assign]

__all__ = ["Repository", "InvalidTransition", "DB_OUTAGE_ERRORS"]
