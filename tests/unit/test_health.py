"""Health must mean healthy now: silent components turn red, entries that no longer apply disappear.

Live check 2026-10-02: the database entry was written once at startup and stayed green for 17 h, and a venue-pair entry
stayed green 7.8 h after anything had last checked that pair.
"""

from app.core.clock import ManualClock
from app.core.health import Health
from tests.unit.helpers import T0


def test_a_component_that_stops_reporting_turns_red():
    clock = ManualClock(T0)
    h = Health(clock)
    h.mark("engine", True, "3 active trades", stale_after=180)
    clock.advance(seconds=179)
    assert h.all_ok and h.snapshot()["engine"]["ok"]
    clock.advance(seconds=2)
    snap = h.snapshot()["engine"]
    assert snap["ok"] is False and "no report for 3 min" in snap["detail"] and "3 active trades" in snap["detail"]
    assert not h.all_ok
    h.mark("engine", True, "3 active trades", stale_after=180)  # reporting again: green again
    assert h.all_ok


def test_an_entry_that_is_no_longer_refreshed_is_dropped_not_kept_green_or_red():
    clock = ManualClock(T0)
    h = Health(clock)
    h.mark("venue:Gate/BloFin USDT", False, "timed out", expire_after=3600)
    h.mark("scanner", True, "ok")
    assert not h.all_ok
    clock.advance(seconds=3601)
    assert "venue:Gate/BloFin USDT" not in h.snapshot() and h.all_ok


def test_components_without_limits_keep_their_last_report():
    clock = ManualClock(T0)
    h = Health(clock)
    h.mark("zero_delta", True, "last sweep ok")
    clock.advance(days=3)
    assert h.snapshot()["zero_delta"]["ok"] and h.snapshot()["zero_delta"]["age_seconds"] == 3 * 86400


def test_database_health_follows_the_repository_not_the_startup_mark():
    from app.workers.runner import Runner
    from tests.chaos.test_chaos import Rig, World

    w = World()
    runner = Runner(settings=None, params=w.params, repo=w.repo, engine=w.engine, scanner=Rig().scanner,
                    notifier=w.notifier, health=Health(w.clock), clock=w.clock)
    runner.mark_database_health()
    assert runner.health.snapshot()["database"]["ok"]
    w.repo.db_ok = False
    w.repo._backlog.extend([None, None])
    runner.mark_database_health()
    snap = runner.health.snapshot()["database"]
    assert snap["ok"] is False and "2 writes buffered" in snap["detail"]
