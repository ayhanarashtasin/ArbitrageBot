"""Logging: rotating files keep the detail, the console shows only what needs a person's attention.

Files (in LOG_DIR, default <project>/logs; each rotates at LOG_MAX_MB and keeps LOG_BACKUP_COUNT old files):

  bot.log   the bot's own messages at LOG_LEVEL (default INFO): decisions, rejections, alerts, warnings, tracebacks
  http.log  every HTTP request the bot makes (httpx) and every call to its own API (uvicorn access), secrets masked

Console: warnings and errors, a few milestone alerts (startup, recovery, trades opened and closed, pair selections)
and uvicorn's own start and stop lines. HTTP lines never reach the console. CONSOLE_LOG_LEVEL=INFO adds the rest of
bot.log; it never adds the HTTP lines (they are in http.log).

Why a separate http.log: httpx logs one INFO line per request (about five a second with open trades), which buried
every real message in the console, and it logs the full URL, which for signed exchange calls carries the signature and
for Telegram the bot token. Both are masked before anything is written.
"""

import logging
import re
import sys
import time
from logging.handlers import RotatingFileHandler
from pathlib import Path

from pydantic import Field, ValidationError, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

FORMAT = "%(asctime)s %(levelname)s %(name)s: %(message)s"
PROJECT_ROOT = Path(__file__).resolve().parents[2]

# Loggers whose records go to http.log instead of bot.log, and never to the console below WARNING.
HTTP_LOGGERS = ("httpx", "httpcore", "uvicorn.access")
# Alerts worth a line on the console even though they are INFO (the notifier tags each record with its kind).
MILESTONE_ALERTS = frozenset({"startup", "recovery", "entry_started", "entry_completed", "trade_closed", "final_pnl",
                              "pairs_selected", "kill_switch"})

_SECRET_PARAM = re.compile(
    r"(?i)([?&](?:signature|sign|api[_-]?key|access[_-]?key|token|secret|passphrase|listenkey)=)[^&\s\"']+")
_TELEGRAM_TOKEN = re.compile(r"/bot[0-9]+:[A-Za-z0-9_-]+")
_LEVELS = {"DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"}


def mask_secrets(text: str) -> str:
    """Signatures, API keys and the Telegram bot token never reach a log file."""
    return _TELEGRAM_TOKEN.sub("/bot***", _SECRET_PARAM.sub(r"\1***", text))


class LogSettings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_ignore_empty=True, extra="ignore")

    log_level: str = "INFO"  # what bot.log records
    console_log_level: str = "WARNING"  # what the console shows besides milestone alerts
    log_dir: Path | None = None  # default: <project>/logs
    log_max_mb: int = Field(default=20, ge=1, le=500)
    log_backup_count: int = Field(default=10, ge=1, le=100)

    @field_validator("log_level", "console_log_level")
    @classmethod
    def _level(cls, v: str) -> str:
        v = v.strip().upper()
        if v not in _LEVELS:
            raise ValueError(f"must be one of {sorted(_LEVELS)}")
        return v


class SafeRotatingFileHandler(RotatingFileHandler):
    """Never lets a failed rotation break logging.

    On Windows a file another program holds open (an editor, a second bot) cannot be renamed. The stock handler then
    raises on every record; this one keeps appending to the current file and tries again a minute later.
    """

    _retry_at = 0.0

    def shouldRollover(self, record: logging.LogRecord) -> bool:
        return time.monotonic() >= self._retry_at and super().shouldRollover(record)

    def doRollover(self) -> None:
        try:
            super().doRollover()
        except OSError:
            self._retry_at = time.monotonic() + 60
            if self.stream is None:
                self.stream = self._open()


class _NotHttp(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        return not record.name.startswith(HTTP_LOGGERS)


class _MaskedFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        return mask_secrets(super().format(record))


class _Console(logging.Filter):
    """What the console shows: warnings and up, plus milestone alerts. HTTP lines only when they are warnings."""

    def __init__(self, level: int):
        super().__init__()
        self.level = level

    def filter(self, record: logging.LogRecord) -> bool:
        floor = max(self.level, logging.WARNING) if record.name.startswith(HTTP_LOGGERS) else self.level
        if record.levelno >= floor:
            return True
        return (self.level <= logging.WARNING and record.levelno >= logging.INFO
                and getattr(record, "alert_kind", None) in MILESTONE_ALERTS)


# (logger, handler) pairs installed by the last configure_logging() call, so a second call replaces them.
_installed: list[tuple[logging.Logger, logging.Handler]] = []


def _attach(logger: logging.Logger, handler: logging.Handler) -> None:
    logger.addHandler(handler)
    _installed.append((logger, handler))


def _remove_installed() -> None:
    while _installed:
        logger, handler = _installed.pop()
        logger.removeHandler(handler)
        handler.close()


def load_log_settings() -> LogSettings:
    try:
        return LogSettings()
    except ValidationError as exc:  # a typo in .env must not stop the bot from starting
        first = exc.errors()[0]
        print(f"WARNING: invalid logging setting {first['loc'][0]!r} ({first['msg']}): using the defaults",
              file=sys.stderr)
        return LogSettings.model_construct()  # the field defaults, without reading the environment again


def configure_logging(settings: LogSettings | None = None) -> Path | None:
    """Install the handlers. Safe to call again (uvicorn imports the app module twice under `python -m app.main`).

    Returns the log directory, or None when it cannot be written (the console still works).
    """
    s = settings or load_log_settings()
    _remove_installed()
    file_level, console_level = getattr(logging, s.log_level), getattr(logging, s.console_log_level)

    root = logging.getLogger()
    root.setLevel(min(file_level, console_level, logging.INFO))  # milestone alerts are INFO; the handlers filter
    for old in list(root.handlers):  # the plain handler a logging.basicConfig() call left behind
        if type(old) is logging.StreamHandler:
            root.removeHandler(old)

    console = logging.StreamHandler(sys.stderr)
    console.setFormatter(logging.Formatter(FORMAT))
    console.addFilter(_Console(console_level))
    try:  # a coin name such as 牛来 must not turn into a "Logging error" block on a narrow console encoding
        sys.stderr.reconfigure(errors="backslashreplace")  # type: ignore[union-attr]
    except (AttributeError, ValueError):
        pass
    _attach(root, console)

    log_dir = s.log_dir or PROJECT_ROOT / "logs"
    if not log_dir.is_absolute():
        log_dir = PROJECT_ROOT / log_dir
    try:
        log_dir.mkdir(parents=True, exist_ok=True)
        size, keep = s.log_max_mb * 1024 * 1024, s.log_backup_count

        main_file = SafeRotatingFileHandler(log_dir / "bot.log", maxBytes=size, backupCount=keep, encoding="utf-8",
                                            delay=True)
        main_file.setLevel(file_level)
        main_file.setFormatter(_MaskedFormatter(FORMAT))
        main_file.addFilter(_NotHttp())
        _attach(root, main_file)
        _attach(logging.getLogger("uvicorn"), main_file)  # uvicorn does not propagate to the root logger

        http_file = SafeRotatingFileHandler(log_dir / "http.log", maxBytes=size, backupCount=3, encoding="utf-8",
                                            delay=True)
        http_file.setLevel(logging.DEBUG)
        http_file.setFormatter(_MaskedFormatter(FORMAT))
    except OSError as exc:
        logging.getLogger("app.startup").warning("cannot write log files in %s (%s): console only", log_dir, exc)
        log_dir, http_file = None, None  # type: ignore[assignment]

    for name in HTTP_LOGGERS:
        logger = logging.getLogger(name)
        for old in list(logger.handlers):  # uvicorn's own access handler would still print every request
            logger.removeHandler(old)
        logger.propagate = False  # keeps these lines out of bot.log and out of the root console handler
        if name != "httpcore":  # httpcore only logs connection traces at DEBUG
            logger.setLevel(logging.INFO)
        if http_file is not None:
            _attach(logger, http_file)
        _attach(logger, console)  # only warnings get through the console filter for these names

    if log_dir is not None:
        logging.getLogger("app.startup").info(
            "logging to %s: bot.log (everything the bot decides), http.log (requests); console shows warnings, "
            "errors and milestones - CONSOLE_LOG_LEVEL=INFO for more", log_dir,
            extra={"alert_kind": "startup"})
    return log_dir
