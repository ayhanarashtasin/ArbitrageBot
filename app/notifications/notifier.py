"""Alerts (project.md section 48). Every alert is stored first; Telegram delivery is optional and best effort.

Alerts never contain secrets: message text is scrubbed of any configured secret value before storage or
sending, and delivery failures are recorded without the request URL (which contains the bot token).
"""

import logging

import httpx

from app.persistence.repository import Repository

log = logging.getLogger(__name__)

SEVERITIES = ("info", "warning", "critical")

# Kinds required by section 48. The engine may only raise kinds from this list.
ALERT_KINDS = frozenset({
    "zero_delta_auth_failure", "zero_delta_stale_data", "zero_delta_outage", "candidate_found",
    "candidate_rejected", "direct_venue_mismatch", "waiting_for_low_volatility", "entry_started", "entry_paused",
    "entry_resumed", "entry_completed", "partial_fill", "one_leg_failure", "delta_imbalance", "venue_disconnect",
    "stale_direct_data", "margin_warning", "liquidation_warning", "controlled_exit_30pct", "emergency_exit_10pct",
    "trade_closed", "funding_payment", "final_pnl", "critical_error", "manual_intervention_required",
    "kill_switch", "startup", "recovery", "pairs_selected",
})


class Notifier:
    def __init__(self, repo: Repository, *, telegram_enabled: bool = False, bot_token: str | None = None,
                 chat_id: str | None = None, secrets: list[str] | None = None,
                 transport: httpx.AsyncBaseTransport | None = None):
        self._repo = repo
        self._enabled = telegram_enabled and bool(bot_token and chat_id)
        self._token, self._chat = bot_token, chat_id
        self._secrets = [s for s in (secrets or []) if s]
        if bot_token:
            self._secrets.append(bot_token)
        self._transport = transport

    def scrub(self, text: str) -> str:
        for secret in self._secrets:
            text = text.replace(secret, "***")
        return text

    async def alert(self, kind: str, message: str, *, severity: str = "info", trade_id: str | None = None) -> None:
        if kind not in ALERT_KINDS:
            raise ValueError(f"unknown alert kind {kind!r}")
        if severity not in SEVERITIES:
            raise ValueError(f"unknown severity {severity!r}")
        text = self.scrub(message)
        alert_id = self._repo.add_alert(kind, severity, text, trade_id)
        log.log({"info": logging.INFO, "warning": logging.WARNING, "critical": logging.ERROR}[severity],
                "[%s] %s", kind, text, extra={"alert_kind": kind})  # the console shows milestone kinds only
        if not self._enabled:
            return
        prefix = {"info": "ℹ️", "warning": "⚠️", "critical": "🚨"}[severity]
        body = {"chat_id": self._chat, "text": f"{prefix} {kind}" + (f" ({trade_id})" if trade_id else "") + f"\n{text}"}
        try:
            async with httpx.AsyncClient(transport=self._transport, timeout=10) as client:
                resp = await client.post(f"https://api.telegram.org/bot{self._token}/sendMessage", json=body)
            if resp.status_code == 200:
                self._repo.mark_alert_delivered(alert_id)
            else:
                log.warning("Telegram delivery failed (HTTP %s)", resp.status_code)
        except httpx.HTTPError as exc:
            log.warning("Telegram delivery failed (%s)", type(exc).__name__)  # never log the URL or token
