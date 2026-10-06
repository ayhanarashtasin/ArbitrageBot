"""Shutdown: a runner that is slow, stuck or failed must never stop the connections from being closed."""

import asyncio
import logging
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from app.api import app as api_app


class Runner:
    def __init__(self, run):
        self._run, self.stopped = run, False

    def stop(self):
        self.stopped = True

    async def run(self):
        await self._run(self)


async def _stop_wait(runner):
    while not runner.stopped:
        await asyncio.sleep(0.005)


async def _never_ends(runner):
    while True:
        await asyncio.sleep(3600)


async def _fails(runner):
    raise RuntimeError("engine blew up")


async def _stop_and_wait(runner):
    task = asyncio.create_task(runner.run())
    await asyncio.sleep(0)
    await api_app.stop_runner(runner, task)
    return task


@pytest.fixture
def quick(monkeypatch):
    monkeypatch.setattr(api_app, "RUNNER_STOP_WAIT_SECONDS", 0.05)
    monkeypatch.setattr(api_app, "RUNNER_CANCEL_WAIT_SECONDS", 0.05)


def test_a_runner_that_stops_when_asked_is_simply_awaited(quick):
    runner = Runner(_stop_wait)
    task = asyncio.run(_stop_and_wait(runner))
    assert runner.stopped and task.done() and not task.cancelled()


def test_a_runner_that_does_not_stop_is_cancelled_and_logged_not_raised(quick, caplog):
    runner = Runner(_never_ends)
    with caplog.at_level(logging.WARNING, logger=api_app.log.name):
        task = asyncio.run(_stop_and_wait(runner))
    assert task.cancelled()
    assert "did not stop" in caplog.text


def test_a_runner_that_failed_is_logged_not_raised(quick, caplog):
    runner = Runner(_fails)
    with caplog.at_level(logging.ERROR, logger=api_app.log.name):
        asyncio.run(_stop_and_wait(runner))
    assert "ended with an error" in caplog.text and "engine blew up" in caplog.text


def test_no_runner_task_is_fine():
    runner = Runner(_stop_wait)
    asyncio.run(api_app.stop_runner(runner, None))
    assert runner.stopped


@pytest.mark.parametrize("behaviour", [_stop_wait, _never_ends, _fails])
def test_the_runtime_is_closed_whatever_the_runner_does(quick, behaviour):
    closed = []

    async def aclose():
        closed.append(True)

    rt = SimpleNamespace(runner=Runner(behaviour), aclose=aclose)
    with TestClient(api_app.create_app(rt)):
        pass
    assert closed == [True] and rt.runner.stopped
