"""One bot per folder: a second start is refused before it touches the database or a venue."""

import os
import subprocess
import sys
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.api import app as api_app
from app.core import instance_lock as il
from app.core.instance_lock import AlreadyRunning, InstanceLock


@pytest.fixture
def lock_path(tmp_path, monkeypatch):
    path = tmp_path / "data" / "bot.lock"
    monkeypatch.setattr(il, "DEFAULT_LOCK_FILE", path)
    return path


def test_a_second_lock_is_refused_and_names_the_holder(lock_path):
    first = InstanceLock(lock_path)
    first.acquire()
    try:
        with pytest.raises(AlreadyRunning, match=f"process {os.getpid()}"):
            InstanceLock(lock_path).acquire()
    finally:
        first.release()


def test_the_lock_can_be_taken_again_after_release(lock_path):
    first = InstanceLock(lock_path)
    first.acquire()
    first.release()
    again = InstanceLock(lock_path)
    again.acquire()
    again.release()


def test_acquire_and_release_twice_are_harmless(lock_path):
    lock = InstanceLock(lock_path)
    lock.acquire()
    lock.acquire()
    lock.release()
    lock.release()
    other = InstanceLock(lock_path)
    other.acquire()
    other.release()


def test_a_process_that_dies_leaves_no_stale_lock(lock_path):
    code = ("import sys, time; from pathlib import Path; from app.core.instance_lock import InstanceLock; "
            "InstanceLock(Path(sys.argv[1])).acquire(); print('held', flush=True); time.sleep(60)")
    holder = subprocess.Popen([sys.executable, "-c", code, str(lock_path)], stdout=subprocess.PIPE, text=True,
                              cwd=Path(__file__).resolve().parents[2])
    try:
        assert holder.stdout.readline().strip() == "held"
        with pytest.raises(AlreadyRunning, match=f"process {holder.pid}"):
            InstanceLock(lock_path).acquire()
    finally:
        holder.kill()
        holder.wait(timeout=10)
    deadline = time.monotonic() + 5
    while True:  # the system frees the lock a moment after the process ends
        lock = InstanceLock(lock_path)
        try:
            lock.acquire()
            break
        except AlreadyRunning:
            if time.monotonic() > deadline:
                raise
            time.sleep(0.1)
    lock.release()


def test_a_second_start_is_refused_before_the_runtime_is_built(lock_path, monkeypatch):
    def not_built():
        raise AssertionError("the database and the venues must not be touched by a refused start")

    monkeypatch.setattr(api_app, "build_runtime", not_built)
    holder = InstanceLock(lock_path)
    holder.acquire()
    try:
        with pytest.raises(AlreadyRunning), TestClient(api_app.create_app(start_runner=False)):
            pass
    finally:
        holder.release()


def test_a_real_start_holds_the_lock_while_running_and_frees_it_after(lock_path, monkeypatch):
    from tests.unit.test_runtime import runtime
    monkeypatch.setattr(api_app, "build_runtime", lambda: runtime())
    with TestClient(api_app.create_app(start_runner=False)), pytest.raises(AlreadyRunning):
        InstanceLock(lock_path).acquire()
    again = InstanceLock(lock_path)
    again.acquire()
    again.release()
