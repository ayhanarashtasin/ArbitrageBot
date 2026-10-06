"""Logging: full detail in rotating files, a quiet console, no secrets in any file."""

import logging
import logging.handlers

import pytest

from app.core import logging_setup as ls

HTTP_LOGGERS = ls.HTTP_LOGGERS


@pytest.fixture(autouse=True)
def restore_logging():
    """configure_logging() edits the root and the HTTP loggers; put everything back so other tests are unaffected."""
    root = logging.getLogger()
    saved = {"root": (root.level, list(root.handlers))}
    for name in (*HTTP_LOGGERS, "uvicorn"):
        lg = logging.getLogger(name)
        saved[name] = (lg.level, list(lg.handlers), lg.propagate)
    yield
    ls._remove_installed()
    root.setLevel(saved["root"][0])
    root.handlers[:] = saved["root"][1]
    for name in (*HTTP_LOGGERS, "uvicorn"):
        lg = logging.getLogger(name)
        lg.setLevel(saved[name][0])
        lg.handlers[:] = saved[name][1]
        lg.propagate = saved[name][2]


def configure(tmp_path, **kw):
    settings = ls.LogSettings(_env_file=None, log_dir=tmp_path / "logs", **kw)
    return ls.configure_logging(settings)


def read(path):
    for h in logging.getLogger().handlers + logging.getLogger("httpx").handlers:
        h.flush()
    return path.read_text(encoding="utf-8") if path.exists() else ""


def test_http_lines_go_to_http_log_only_and_the_console_stays_quiet(tmp_path, capsys):
    log_dir = configure(tmp_path)
    capsys.readouterr()  # drop the one-line startup notice
    logging.getLogger("httpx").info(
        'HTTP Request: GET https://api.gateio.ws/api/v4/futures/usdt/order_book "HTTP/1.1 200 OK"')
    logging.getLogger("uvicorn.access").info('127.0.0.1:5000 - "GET /api/overview HTTP/1.1" 200')
    logging.getLogger("app.workers.scanner").info("candidate rejected MON long OKX / short Hyperliquid: expected_net_edge")
    assert capsys.readouterr().err == ""  # nothing at all on the console
    http, bot = read(log_dir / "http.log"), read(log_dir / "bot.log")
    assert "order_book" in http and "GET /api/overview" in http and "candidate rejected" not in http
    assert "candidate rejected" in bot and "order_book" not in bot and "/api/overview" not in bot


def test_warnings_errors_and_milestone_alerts_reach_the_console_and_the_file(tmp_path, capsys):
    log_dir = configure(tmp_path)
    capsys.readouterr()
    log = logging.getLogger("app.notifications.notifier")
    log.info("[funding_payment] long okx_swap: -0.0183 USD", extra={"alert_kind": "funding_payment"})  # routine
    log.info("[trade_closed] closed: edge_decayed", extra={"alert_kind": "trade_closed"})  # milestone
    logging.getLogger("app.workers.scanner").warning("verification of OKX/Binance failed: timeout")
    logging.getLogger("app.execution.engine").error("trade arb0006 step failed")
    err = capsys.readouterr().err
    assert "trade_closed" in err and "verification of OKX/Binance failed" in err and "step failed" in err
    assert "funding_payment" not in err
    bot = read(log_dir / "bot.log")
    assert "funding_payment" in bot and "trade_closed" in bot and "step failed" in bot  # the file has everything


def test_console_level_info_shows_the_bot_lines_but_still_never_the_http_lines(tmp_path, capsys):
    configure(tmp_path, console_log_level="INFO")
    capsys.readouterr()
    logging.getLogger("app.workers.scanner").info("candidate rejected EVAA")
    logging.getLogger("httpx").info("HTTP Request: GET https://example.test/x")
    err = capsys.readouterr().err
    assert "candidate rejected EVAA" in err and "HTTP Request" not in err


def test_log_level_controls_what_bot_log_records(tmp_path):
    log_dir = configure(tmp_path, log_level="WARNING")
    logging.getLogger("app.workers.scanner").info("an info line")
    logging.getLogger("app.workers.scanner").warning("a warning line")
    bot = read(log_dir / "bot.log")
    assert "a warning line" in bot and "an info line" not in bot


def test_secrets_are_masked_in_both_files(tmp_path):
    log_dir = configure(tmp_path)
    logging.getLogger("httpx").info(
        'HTTP Request: GET https://fapi.binance.com/fapi/v1/order?symbol=GRVTUSDT&timestamp=1&signature=deadbeef99'
        ' "HTTP/1.1 200 OK"')
    logging.getLogger("httpx").info(
        'HTTP Request: POST https://api.telegram.org/bot123456:ABC-def_GHI/sendMessage "HTTP/1.1 200 OK"')
    logging.getLogger("app.exchanges").warning("retry https://x.test/y?api_key=SECRETKEY&limit=5")
    http, bot = read(log_dir / "http.log"), read(log_dir / "bot.log")
    for text in (http, bot):
        assert "deadbeef99" not in text and "123456:ABC" not in text and "SECRETKEY" not in text
    assert "symbol=GRVTUSDT" in http and "signature=***" in http and "/bot***/sendMessage" in http
    assert "api_key=***" in bot and "limit=5" in bot


def test_calling_configure_twice_does_not_duplicate_lines(tmp_path):
    configure(tmp_path)
    log_dir = configure(tmp_path)  # uvicorn imports app.main twice under `python -m app.main`
    logging.getLogger("app.workers.scanner").warning("only once")
    assert read(log_dir / "bot.log").count("only once") == 1


def test_non_ascii_coin_names_are_written_as_utf8(tmp_path):
    log_dir = configure(tmp_path)
    logging.getLogger("app.workers.scanner").info("selected 牛来 long Binance USDT / short Gate")
    assert "牛来" in read(log_dir / "bot.log")


def test_an_unwritable_log_directory_leaves_a_working_console(tmp_path, capsys):
    blocker = tmp_path / "not_a_dir"
    blocker.write_text("a file where the log directory should be")
    settings = ls.LogSettings(_env_file=None, log_dir=blocker / "logs")
    assert ls.configure_logging(settings) is None
    assert "cannot write log files" in capsys.readouterr().err
    logging.getLogger("app.workers.scanner").warning("still visible")
    assert "still visible" in capsys.readouterr().err


def test_a_relative_log_dir_is_anchored_to_the_project_not_the_working_directory(tmp_path, monkeypatch):
    project = tmp_path / "project"
    monkeypatch.setattr(ls, "PROJECT_ROOT", project)
    monkeypatch.chdir(tmp_path)
    settings = ls.LogSettings(_env_file=None, log_dir="some_logs")
    assert ls.configure_logging(settings) == project / "some_logs"
    assert not (tmp_path / "some_logs").exists()  # not relative to where the bot happened to be started
    logging.getLogger("app.workers.scanner").warning("anchored")
    assert "anchored" in read(project / "some_logs" / "bot.log")


def test_the_default_log_directory_is_logs_in_the_project():
    assert ls.LogSettings(_env_file=None).log_dir is None  # configure_logging() then uses PROJECT_ROOT / "logs"
    assert (ls.PROJECT_ROOT / "app" / "core" / "logging_setup.py").exists()


def test_a_bad_logging_value_in_the_environment_falls_back_to_the_defaults(monkeypatch, tmp_path, capsys):
    monkeypatch.chdir(tmp_path)  # no .env here
    monkeypatch.setenv("LOG_LEVEL", "LOUD")
    s = ls.load_log_settings()
    assert s.log_level == "INFO" and s.console_log_level == "WARNING"
    assert "invalid logging setting 'log_level'" in capsys.readouterr().err


def test_log_levels_are_case_insensitive():
    s = ls.LogSettings(_env_file=None, log_level="debug", console_log_level="Info")
    assert (s.log_level, s.console_log_level) == ("DEBUG", "INFO")


def test_a_file_another_program_holds_open_does_not_break_logging(tmp_path, monkeypatch, capsys):
    """On Windows a log file an editor has open cannot be renamed: the stock handler then fails on every record."""
    path = tmp_path / "bot.log"
    handler = ls.SafeRotatingFileHandler(path, maxBytes=50, backupCount=2, encoding="utf-8", delay=True)

    def locked(self):
        raise PermissionError("the file is being used by another process")

    monkeypatch.setattr(logging.handlers.RotatingFileHandler, "doRollover", locked)
    logger = logging.getLogger("rotation_test")
    logger.propagate = False
    logger.addHandler(handler)
    try:
        for i in range(10):
            logger.warning("record number %d with some padding to pass the size limit", i)
    finally:
        logger.removeHandler(handler)
        handler.close()
    assert "Logging error" not in capsys.readouterr().err
    assert path.read_text(encoding="utf-8").count("record number") == 10  # nothing lost, nothing raised
    assert handler._retry_at > 0  # it backs off instead of retrying the rename on every record


def test_the_file_rotates_at_the_size_limit_and_keeps_the_newest(tmp_path):
    path = tmp_path / "bot.log"
    handler = ls.SafeRotatingFileHandler(path, maxBytes=200, backupCount=2, encoding="utf-8", delay=True)
    logger = logging.getLogger("rotation_test2")
    logger.propagate = False
    logger.addHandler(handler)
    try:
        for i in range(40):
            logger.warning("record %02d padding padding padding", i)
    finally:
        logger.removeHandler(handler)
        handler.close()
    assert sorted(p.name for p in tmp_path.iterdir()) == ["bot.log", "bot.log.1", "bot.log.2"]
    assert "record 39" in path.read_text(encoding="utf-8")
