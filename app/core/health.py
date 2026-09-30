"""Health registry (project.md section 49). Components report success or failure; the API reads it.

Nothing here contains secrets. Details are short human-readable strings.
"""

from dataclasses import dataclass
from datetime import datetime

from app.core.clock import Clock


@dataclass
class ComponentHealth:
    ok: bool
    detail: str
    updated_at: datetime


class Health:
    def __init__(self, clock: Clock):
        self.clock = clock
        self._components: dict[str, ComponentHealth] = {}

    def mark(self, name: str, ok: bool, detail: str = "") -> None:
        self._components[name] = ComponentHealth(ok, detail[:200], self.clock.now())

    def snapshot(self) -> dict[str, dict]:
        now = self.clock.now()
        return {name: {"ok": c.ok, "detail": c.detail, "age_seconds": round((now - c.updated_at).total_seconds(), 1)}
                for name, c in sorted(self._components.items())}

    @property
    def all_ok(self) -> bool:
        return bool(self._components) and all(c.ok for c in self._components.values())
