"""Kill switch (project.md section 50).

PAUSE_NEW_ENTRIES stops new positions but keeps risk management running.
EMERGENCY_FLATTEN cancels entry work and starts protected closing of every open trade.
Every change needs an actor and a reason, is timestamped, and is reported to the audit sink.
Authentication happens at the API boundary (app/api); this class only records who did what.
"""

from collections.abc import Callable
from dataclasses import dataclass, field

from app.core.clock import Clock


@dataclass
class KillSwitch:
    clock: Clock
    audit: Callable[[str, dict], None] = lambda kind, data: None
    pause_new_entries: bool = False
    emergency_flatten: bool = False
    history: list[dict] = field(default_factory=list)

    def _record(self, action: str, actor: str, reason: str) -> None:
        entry = {"at": self.clock.now().isoformat(), "action": action, "actor": actor, "reason": reason}
        self.history.append(entry)
        self.audit("kill_switch", entry)

    def pause(self, actor: str, reason: str) -> None:
        self.pause_new_entries = True
        self._record("PAUSE_NEW_ENTRIES", actor, reason)

    def resume(self, actor: str, reason: str) -> None:
        if self.emergency_flatten:
            raise RuntimeError("EMERGENCY_FLATTEN is latched; clear it explicitly first")
        self.pause_new_entries = False
        self._record("RESUME_ENTRIES", actor, reason)

    def flatten(self, actor: str, reason: str) -> None:
        self.emergency_flatten = True
        self.pause_new_entries = True  # flattening always implies no new entries
        self._record("EMERGENCY_FLATTEN", actor, reason)

    def clear_flatten(self, actor: str, reason: str) -> None:
        self.emergency_flatten = False
        self._record("CLEAR_EMERGENCY_FLATTEN", actor, reason)

    @property
    def entries_allowed(self) -> bool:
        return not (self.pause_new_entries or self.emergency_flatten)
