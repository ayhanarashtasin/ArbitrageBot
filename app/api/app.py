"""HTTP API: health, state and the authenticated kill switch (project.md sections 49, 50).

/health and /state are read-only and contain no secrets. Admin actions need the X-Admin-Token header to match
ADMIN_API_TOKEN; when that variable is unset the admin endpoints are disabled entirely.
"""

import asyncio
import hmac
from contextlib import asynccontextmanager

from pathlib import Path

from fastapi import Depends, FastAPI, Header, HTTPException
from fastapi.responses import FileResponse, Response
from pydantic import BaseModel, Field

from app.api import dashboard as dash
from app.bootstrap import Runtime, build_runtime

STATIC = Path(__file__).resolve().parent / "static"


class AdminAction(BaseModel):
    actor: str = Field(min_length=1, max_length=64)
    reason: str = Field(min_length=3, max_length=300)


def create_app(runtime: Runtime | None = None, *, start_runner: bool = True) -> FastAPI:
    holder: dict[str, Runtime] = {}

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        rt = runtime or build_runtime()
        holder["rt"] = rt
        task = asyncio.create_task(rt.runner.run()) if start_runner else None
        try:
            yield
        finally:
            rt.runner.stop()
            if task is not None:
                await asyncio.wait_for(task, timeout=20) if not task.done() else None
            await rt.aclose()

    app = FastAPI(title="Funding arbitrage bot", lifespan=lifespan)

    def rt() -> Runtime:
        if "rt" not in holder:
            if runtime is None:
                raise HTTPException(503, "starting")
            holder["rt"] = runtime
        return holder["rt"]

    def admin(x_admin_token: str | None = Header(default=None)) -> None:
        expected = rt().settings.admin_api_token
        if not expected:
            raise HTTPException(403, "admin API is disabled: ADMIN_API_TOKEN is not set")
        if x_admin_token is None or not hmac.compare_digest(x_admin_token.encode(), expected.encode()):
            raise HTTPException(401, "invalid admin token")

    @app.get("/", include_in_schema=False)
    def home():
        r = rt()
        return {
            "bot": "funding arbitrage bot",
            "run_mode": r.params.run_mode,
            "live_trading_enabled": False,
            "active_trades": len(r.engine.active),
            "entries_allowed": r.kill.entries_allowed,
            "links": {"dashboard": "/dashboard", "health": "/health", "state": "/state", "api_docs": "/docs"},
        }

    # ---- read-only dashboard: a page plus JSON it polls. No secrets, no actions. -----------------
    @app.get("/dashboard", include_in_schema=False)
    def dashboard_page():
        return FileResponse(STATIC / "dashboard.html", headers={"Cache-Control": "no-store"})

    @app.get("/favicon.ico", include_in_schema=False)
    def favicon():
        return Response(status_code=204)

    @app.get("/api/overview")
    def api_overview():
        return dash.overview(rt())

    @app.get("/api/decisions")
    def api_decisions(limit: int = 60):
        return dash.decisions(rt(), max(1, min(limit, 300)))

    @app.get("/api/opportunities")
    def api_opportunities():
        return dash.opportunities(rt())

    @app.get("/api/selection")
    def api_selection():
        return dash.selection(rt())

    @app.get("/api/selection/track-record")
    def api_selection_track_record(limit: int = 60):
        return rt().repo.selection_track_record(max(1, min(limit, 300)))

    @app.get("/api/alerts")
    def api_alerts(limit: int = 40):
        return dash.alerts(rt(), max(1, min(limit, 200)))

    @app.get("/api/performance")
    def api_performance():
        return dash.performance(rt())

    @app.get("/api/trades")
    def api_trades():
        return dash.trades(rt())

    @app.get("/api/trades/{trade_id}")
    def api_trade(trade_id: str):
        try:
            return dash.trade_detail(rt(), trade_id)
        except KeyError as exc:
            raise HTTPException(404, "unknown trade") from exc

    @app.get("/health")
    def health():
        r = rt()
        return {"ok": r.health.all_ok, "run_mode": r.params.run_mode, "live_trading_enabled": False,
                "components": r.health.snapshot()}

    @app.get("/state")
    def state():
        r = rt()
        blocked = r.engine.blocked()
        return {
            "run_mode": r.params.run_mode,
            "kill_switch": {"pause_new_entries": r.kill.pause_new_entries, "emergency_flatten": r.kill.emergency_flatten},
            "new_trades_blocked": blocked,
            "active_trades": r.engine.summary(),
            "last_scans": {a: {"pairs": s.pairs_found, "verified": s.verified, "opened": s.opened,
                               "outcomes": s.outcomes, "note": s.note} for a, s in r.scanner.last_report.items()},
            "paper_defaults_in_use": sorted(k for k, v in r.params.provenance.items() if v == "paper_default"),
        }

    @app.post("/admin/pause-new-entries", dependencies=[Depends(admin)])
    def pause(body: AdminAction):
        rt().kill.pause(body.actor, body.reason)
        return {"pause_new_entries": True}

    @app.post("/admin/resume-entries", dependencies=[Depends(admin)])
    def resume(body: AdminAction):
        try:
            rt().kill.resume(body.actor, body.reason)
        except RuntimeError as exc:
            raise HTTPException(409, str(exc)) from exc
        return {"pause_new_entries": False}

    @app.post("/admin/emergency-flatten", dependencies=[Depends(admin)])
    def flatten(body: AdminAction):
        rt().kill.flatten(body.actor, body.reason)
        return {"emergency_flatten": True}

    @app.post("/admin/clear-emergency-flatten", dependencies=[Depends(admin)])
    def clear(body: AdminAction):
        rt().kill.clear_flatten(body.actor, body.reason)
        return {"emergency_flatten": False, "note": "new entries stay paused until resume-entries"}

    return app
