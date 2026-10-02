import asyncio
import logging

import httpx
import pytest
from fastapi.testclient import TestClient

from app.api.app import create_app
from app.bootstrap import build_runtime
from app.core.clock import ManualClock
from app.core.config import ZeroDeltaSettings
from app.core.trading_config import LIVE_CONFIRMATION_PHRASE, LiveGateError, TradingSettings
from app.notifications.notifier import Notifier
from app.persistence.repository import Repository
from tests.unit.helpers import T0
from tests.unit.test_trading_config import full_live_values

TOKEN = "s3cret-admin-token"


def runtime(tmp_path=None, credentials=None, **kw):
    url = "sqlite://" if tmp_path is None else f"sqlite:///{tmp_path}/bot.db"
    settings = TradingSettings(_env_file=None, database_url=url, **kw)
    from app.exchanges.private.credentials import LiveCredentials
    return build_runtime(settings, ZeroDeltaSettings(_env_file=None), source_for_venue=lambda name: None,
                         credentials=credentials or LiveCredentials(_env_file=None))


def live_kw():
    return dict(run_mode="live", live_trading_enabled=True, live_trading_confirmation=LIVE_CONFIRMATION_PHRASE,
                **full_live_values())


# ---- bootstrap and the live gate -------------------------------------------------------------

def test_paper_runtime_builds():
    rt = runtime()
    assert rt.params.run_mode == "paper" and rt.zd is None and rt.engine.active == {}


def test_live_mode_is_refused_without_the_gate():
    with pytest.raises(LiveGateError, match="LIVE_TRADING_ENABLED"):
        runtime(run_mode="live")


def test_live_mode_with_every_value_still_needs_credentials_for_two_venues():
    from app.exchanges.private.credentials import LiveCredentials
    with pytest.raises(LiveGateError, match="at least two venues"):
        runtime(**live_kw())
    one = LiveCredentials(_env_file=None, binance_api_key="k", binance_api_secret="s")
    with pytest.raises(LiveGateError, match="at least two venues"):
        runtime(credentials=one, **live_kw())


def test_live_runtime_builds_with_two_venues_and_scrubs_their_secrets_from_alerts():
    from app.exchanges.private.credentials import LiveCredentials
    from app.execution.venue_provider import LiveVenueProvider
    creds = LiveCredentials(_env_file=None, binance_api_key="bkey", binance_api_secret="bsecret-123",
                            bybit_api_key="ykey", bybit_api_secret="ysecret-456")
    rt = runtime(credentials=creds, **live_kw())
    assert isinstance(rt.engine.provider, LiveVenueProvider)
    assert set(rt.private_clients) == {"binance_usdt_m", "bybit_linear"}
    assert rt.engine.provider.ready == set()  # nothing may trade before the readiness check has passed
    assert rt.scanner.execution_venue("Binance USDT") is False
    assert rt.notifier.scrub("leak bsecret-123 and ysecret-456") == "leak *** and ***"
    asyncio.run(rt.aclose())


def test_testnet_is_refused():
    with pytest.raises(LiveGateError, match="not implemented"):
        runtime(run_mode="testnet")


def test_kill_switch_survives_a_restart(tmp_path):
    rt = runtime(tmp_path)
    rt.kill.pause("op", "maintenance")
    rt.kill.flatten("op", "incident")
    rt2 = runtime(tmp_path)  # a new process, same database
    assert rt2.kill.pause_new_entries and rt2.kill.emergency_flatten and not rt2.kill.entries_allowed


def test_startup_alert_reports_paper_defaults_honestly():
    rt = runtime()
    asyncio.run(rt.runner.startup())
    msg = rt.repo.alerts("startup")[0].message
    assert "live disabled" in msg and "paper defaults" in msg


# ---- notifications never leak secrets --------------------------------------------------------

def test_alert_text_is_scrubbed_of_secrets():
    repo = Repository("sqlite://", ManualClock(T0))
    n = Notifier(repo, secrets=["abc123secret"])
    asyncio.run(n.alert("critical_error", "failed with cookie abc123secret in it", severity="critical"))
    assert "abc123secret" not in repo.alerts()[0].message and "***" in repo.alerts()[0].message


def test_telegram_delivery_and_failure_never_expose_the_token(caplog):
    repo = Repository("sqlite://", ManualClock(T0))
    sent = []

    def handler(request: httpx.Request) -> httpx.Response:
        sent.append(str(request.url))
        return httpx.Response(200, json={"ok": True})

    n = Notifier(repo, telegram_enabled=True, bot_token="123:TOKEN", chat_id="42", transport=httpx.MockTransport(handler))
    asyncio.run(n.alert("trade_closed", "done", trade_id=None))
    assert repo.alerts()[0].delivered and "123:TOKEN" in sent[0]  # the token is only ever in the request itself

    def failing(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("no route", request=request)

    n2 = Notifier(repo, telegram_enabled=True, bot_token="123:TOKEN", chat_id="42", transport=httpx.MockTransport(failing))
    with caplog.at_level(logging.DEBUG):
        asyncio.run(n2.alert("critical_error", "boom", severity="critical"))
    assert "123:TOKEN" not in caplog.text and not repo.alerts()[-1].delivered


def test_unknown_alert_kinds_and_severities_are_rejected():
    n = Notifier(Repository("sqlite://", ManualClock(T0)))
    with pytest.raises(ValueError):
        asyncio.run(n.alert("made_up_kind", "x"))
    with pytest.raises(ValueError):
        asyncio.run(n.alert("startup", "x", severity="loud"))


def test_telegram_is_off_by_default_and_never_called():
    repo = Repository("sqlite://", ManualClock(T0))
    calls = []
    n = Notifier(repo, transport=httpx.MockTransport(lambda r: calls.append(r) or httpx.Response(200)))
    asyncio.run(n.alert("startup", "hello"))
    assert calls == [] and len(repo.alerts()) == 1


# ---- API -------------------------------------------------------------------------------------

def client(admin_token=TOKEN):
    rt = runtime(admin_api_token=admin_token)
    return TestClient(create_app(rt, start_runner=False)), rt


def body(**kw):
    return {"actor": "alice", "reason": "testing the switch", **kw}


def test_health_and_state_are_public_and_show_no_secrets():
    c, rt = client()
    rt.health.mark("database", True, "ok")
    h = c.get("/health").json()
    assert h["live_trading_enabled"] is False and h["run_mode"] == "paper" and "database" in h["components"]
    st = c.get("/state").json()
    assert st["kill_switch"] == {"pause_new_entries": False, "emergency_flatten": False}
    assert st["active_trades"] == [] and "target_leverage" in st["paper_defaults_in_use"]
    assert TOKEN not in c.get("/state").text and TOKEN not in c.get("/health").text


def test_admin_endpoints_need_the_token():
    c, rt = client()
    assert c.post("/admin/pause-new-entries", json=body()).status_code == 401
    assert c.post("/admin/pause-new-entries", json=body(), headers={"X-Admin-Token": "wrong"}).status_code == 401
    assert not rt.kill.pause_new_entries
    r = c.post("/admin/pause-new-entries", json=body(), headers={"X-Admin-Token": TOKEN})
    assert r.status_code == 200 and rt.kill.pause_new_entries


def test_admin_api_is_disabled_when_no_token_is_configured():
    c, rt = client(admin_token=None)
    r = c.post("/admin/emergency-flatten", json=body(), headers={"X-Admin-Token": "anything"})
    assert r.status_code == 403 and not rt.kill.emergency_flatten


def test_flatten_latches_and_resume_is_refused_until_cleared():
    c, rt = client()
    h = {"X-Admin-Token": TOKEN}
    assert c.post("/admin/emergency-flatten", json=body(), headers=h).status_code == 200
    assert c.post("/admin/resume-entries", json=body(), headers=h).status_code == 409
    assert c.post("/admin/clear-emergency-flatten", json=body(), headers=h).status_code == 200
    assert rt.kill.pause_new_entries  # entries stay paused until resumed on purpose
    assert c.post("/admin/resume-entries", json=body(), headers=h).status_code == 200
    assert rt.kill.entries_allowed


def test_admin_actions_require_an_actor_and_a_reason_and_are_audited():
    c, rt = client()
    h = {"X-Admin-Token": TOKEN}
    assert c.post("/admin/pause-new-entries", json={"actor": "a"}, headers=h).status_code == 422
    assert c.post("/admin/pause-new-entries", json=body(reason="x"), headers=h).status_code == 422
    c.post("/admin/pause-new-entries", json=body(), headers=h)
    from app.persistence.models import AuditLog
    with rt.repo.session() as s:
        entries = s.query(AuditLog).filter(AuditLog.kind == "kill_switch").all()
    assert entries and entries[-1].data["actor"] == "alice" and entries[-1].data["action"] == "PAUSE_NEW_ENTRIES"


@pytest.mark.parametrize("scenario,reason", [("normal", "edge_decayed"), ("crash", "margin"), ("partial", "max_hold_reached"),
                                             ("stoploss", "exit reason: equity_at_or_below_controlled_ratio")])
def test_paper_demo_scenarios_run_to_a_closed_trade(scenario, reason, capsys):
    from app.tools.paper_demo import main
    asyncio.run(main(scenario))
    out = capsys.readouterr().out
    assert "state -> CLOSED" in out and reason in out and "total_realized_pnl" in out


def test_home_page_shows_status_and_links():
    c, _ = client()
    body_ = c.get("/").json()
    assert body_["run_mode"] == "paper" and body_["live_trading_enabled"] is False
    assert body_["links"]["state"] == "/state" and TOKEN not in c.get("/").text
