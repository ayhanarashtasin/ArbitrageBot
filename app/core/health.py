"""Health registry (project.md section 49). Components report success or failure; the API reads it.

Nothing here contains secrets. Details are short human-readable strings.

Two optional time limits keep the registry honest (live check 2026-10-02: the database entry was written once at
startup and stayed green for 17 h, and a venue-pair entry stayed green 7.8 h after anything had checked it):
  stale_after   a component that has not reported for this long counts as FAILED: a loop that stops running can never
                stay green on its last good report
  expire_after  an entry that has not been refreshed for this long is dropped: it no longer describes anything the bot
                uses (a venue pair that is no longer selected), so it must neither reassure nor alarm
"""

from dataclasses import dataclass
from datetime import datetime

from app.core.clock import Clock


@dataclass
class ComponentHealth:
    ok: bool
    detail: str
    updated_at: datetime
    stale_after: float | None = None
    expire_after: float | None = None


class Health:
    def __init__(self, clock: Clock):
        self.clock = clock
        self._components: dict[str, ComponentHealth] = {}

    def mark(self, name: str, ok: bool, detail: str = "", *, stale_after: float | None = None,
             expire_after: float | None = None) -> None:
        self._components[name] = ComponentHealth(ok, detail[:200], self.clock.now(), stale_after, expire_after)

    def _current(self) -> dict[str, tuple[bool, str, float]]:
        """name -> (ok, detail, age in seconds) after applying the time limits. Read from API threads too, so it
        iterates over a copy and never fails on a component added meanwhile."""
        now = self.clock.now()
        out = {}
        for name, c in list(self._components.items()):
            age = (now - c.updated_at).total_seconds()
            if c.expire_after is not None and age > c.expire_after:
                self._components.pop(name, None)
                continue
            if c.stale_after is not None and age > c.stale_after:
                out[name] = (False, f"no report for {age / 60:.0f} min (last: {c.detail})"[:200], age)
            else:
                out[name] = (c.ok, c.detail, age)
        return out

    def snapshot(self) -> dict[str, dict]:
        return {name: {"ok": ok, "detail": detail, "age_seconds": round(age, 1)}
                for name, (ok, detail, age) in sorted(self._current().items())}

    @property
    def all_ok(self) -> bool:
        current = self._current()
        return bool(current) and all(ok for ok, _, _ in current.values())
