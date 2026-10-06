"""One bot at a time per project folder.

Two bots on the same database and the same exchange accounts would each rebuild every open trade at start-up, then
both manage it - in live mode both could send the closing orders. The second start is refused before it reads the
database or calls an exchange, whatever port it was given.

The lock is an operating-system file lock on data/bot.lock. The system drops it the moment the process ends, however
it ends (Ctrl+C, a crash, a power cut), so a left-over file never blocks a restart. The holder writes its process id
after the lock byte so the refusal can name it.
"""

import os
import sys
from pathlib import Path

DEFAULT_LOCK_FILE = Path(__file__).resolve().parents[2] / "data" / "bot.lock"
_PID_FIELD = 12  # fixed width, so a shorter pid never leaves digits of an older one behind

if sys.platform == "win32":
    import msvcrt

    def _try_lock(fd: int) -> None:
        os.lseek(fd, 0, os.SEEK_SET)
        msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)

    def _unlock(fd: int) -> None:
        os.lseek(fd, 0, os.SEEK_SET)
        msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
else:
    import fcntl

    def _try_lock(fd: int) -> None:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)

    def _unlock(fd: int) -> None:
        fcntl.flock(fd, fcntl.LOCK_UN)


class AlreadyRunning(RuntimeError):
    """Another bot holds the lock."""


class InstanceLock:
    def __init__(self, path: Path | None = None):
        self.path = path or DEFAULT_LOCK_FILE
        self._fd: int | None = None

    def acquire(self) -> None:
        """Take the lock or raise AlreadyRunning. Taking it twice from one object is a no-op."""
        if self._fd is not None:
            return
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(self.path, os.O_RDWR | os.O_CREAT, 0o644)
        try:
            _try_lock(fd)
        except OSError:
            os.close(fd)
            pid = self.holder_pid()
            who = f"process {pid}" if pid else "another process"
            raise AlreadyRunning(
                f"the bot is already running from this folder ({who}, lock {self.path}). Two bots would both manage "
                "the same trades, so this one will not start. Use the one that is running (its dashboard is at "
                "/dashboard), or stop it first (Ctrl+C in its window) and start again.") from None
        os.lseek(fd, 0, os.SEEK_SET)
        os.write(fd, b"#" + str(os.getpid()).ljust(_PID_FIELD).encode())  # byte 0 is the lock itself
        self._fd = fd

    def release(self) -> None:
        if self._fd is None:
            return
        fd, self._fd = self._fd, None
        try:
            _unlock(fd)
        except OSError:
            pass  # closing the file drops the lock anyway
        finally:
            os.close(fd)

    def holder_pid(self) -> int | None:
        """The process id the current holder wrote, if it can be read."""
        try:
            fd = os.open(self.path, os.O_RDONLY)
        except OSError:
            return None
        try:
            os.lseek(fd, 1, os.SEEK_SET)
            return int(os.read(fd, _PID_FIELD).strip() or 0) or None
        except (OSError, ValueError):
            return None
        finally:
            os.close(fd)
