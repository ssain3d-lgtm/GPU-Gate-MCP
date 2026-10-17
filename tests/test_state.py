import errno
import os
import subprocess
import sys
from datetime import datetime

import pytest

from gpu_gate import state


@pytest.fixture
def home(tmp_path, monkeypatch):
    monkeypatch.setenv("GPU_GATE_HOME", str(tmp_path))
    return state.home()


@pytest.fixture
def dead_pid():
    """The pid of a process that has already ended."""
    p = subprocess.Popen([sys.executable, "-c", "pass"])
    p.wait()
    return p.pid


def test_home_follows_the_env_var_and_creates_subdirs(tmp_path, monkeypatch):
    target = tmp_path / "gate"
    monkeypatch.setenv("GPU_GATE_HOME", str(target))
    home = state.home()
    assert home == target
    assert (target / "batches").is_dir()
    assert (target / "logs").is_dir()
    paths = state.paths(home)
    assert paths.config == target / "config.json"
    assert paths.lock == target / "lock"
    assert paths.state == target / "state.json"
    assert paths.batches_dir == target / "batches"
    assert paths.logs_dir == target / "logs"


def test_home_default_location_without_env_var(tmp_path, monkeypatch):
    monkeypatch.delenv("GPU_GATE_HOME", raising=False)
    if sys.platform == "win32":
        monkeypatch.setenv("LOCALAPPDATA", str(tmp_path))
        expected = tmp_path / "gpu-gate"
    else:
        monkeypatch.setenv("HOME", str(tmp_path))
        expected = tmp_path / ".local" / "share" / "gpu-gate"
    assert state.home() == expected
    assert (expected / "batches").is_dir()
    assert (expected / "logs").is_dir()


def test_write_json_is_atomic_and_round_trips(tmp_path):
    path = tmp_path / "x.json"
    assert state.read_json(path) is None
    data = {"a": 1, "name": "한글", "nested": {"ok": True, "none": None}}
    state.write_json(path, data)
    assert state.read_json(path) == data

    # A failing write must leave the old file intact and no temp file behind.
    with pytest.raises(TypeError):
        state.write_json(path, {"bad": object()})
    assert state.read_json(path) == data
    assert [p.name for p in tmp_path.iterdir()] == ["x.json"]


def test_write_json_retries_replace_while_a_reader_holds_the_file(tmp_path, monkeypatch):
    path = tmp_path / "x.json"
    real_replace = os.replace
    calls = []

    def flaky_replace(src, dst):
        calls.append((src, dst))
        if len(calls) <= 2:
            raise PermissionError(13, "sharing violation")
        real_replace(src, dst)

    monkeypatch.setattr(state.time, "sleep", lambda seconds: None)
    monkeypatch.setattr(os, "replace", flaky_replace)
    state.write_json(path, {"a": 1})
    assert len(calls) == 3
    assert state.read_json(path) == {"a": 1}
    assert [p.name for p in tmp_path.iterdir()] == ["x.json"]


def test_write_json_raises_after_the_last_failed_replace_and_leaves_no_temp_file(tmp_path, monkeypatch):
    path = tmp_path / "x.json"
    state.write_json(path, {"old": True})
    calls = []

    def locked_replace(src, dst):
        calls.append(src)
        raise PermissionError(13, "sharing violation")

    monkeypatch.setattr(state.time, "sleep", lambda seconds: None)
    monkeypatch.setattr(os, "replace", locked_replace)
    with pytest.raises(PermissionError):
        state.write_json(path, {"new": True})
    assert len(calls) == 10
    assert state.read_json(path) == {"old": True}
    assert [p.name for p in tmp_path.iterdir()] == ["x.json"]


def test_write_json_does_not_retry_errors_that_are_not_sharing_violations(tmp_path, monkeypatch):
    calls = []

    def broken_replace(src, dst):
        calls.append(src)
        raise OSError(errno.ENOSPC, "disk full")

    monkeypatch.setattr(state.time, "sleep", lambda seconds: None)
    monkeypatch.setattr(os, "replace", broken_replace)
    with pytest.raises(OSError):
        state.write_json(tmp_path / "x.json", {"a": 1})
    assert len(calls) == 1
    assert list(tmp_path.iterdir()) == []


def test_read_json_returns_none_for_a_corrupt_file(tmp_path):
    path = tmp_path / "x.json"
    path.write_text("{not json", encoding="utf-8")
    assert state.read_json(path) is None


def test_new_batch_id_has_the_documented_shape():
    batch_id = state.new_batch_id()
    stamp, clock, suffix = batch_id.split("-")
    datetime.strptime(stamp + clock, "%Y%m%d%H%M%S")
    assert len(suffix) == 4
    int(suffix, 16)


def test_lock_blocks_a_second_batch_while_pid_is_alive(home):
    first = state.acquire_lock(home, "a")
    assert first is not None
    assert first.batch_id == "a"
    assert first.stale_batch_id is None
    assert state.acquire_lock(home, "b") is None
    lock = state.current_lock(home)
    assert lock["batch_id"] == "a"
    assert lock["pid"] == os.getpid()
    datetime.fromisoformat(lock["started"])

    state.release_lock(home)
    assert state.acquire_lock(home, "b") is not None


def test_stale_lock_is_reclaimed(home, dead_pid):
    paths = state.paths(home)
    state.write_json(paths.lock, {"batch_id": "old", "pid": dead_pid, "started": "2026-01-01T00:00:00+00:00"})
    state.write_json(state.batch_path(home, "old"), {"batch_id": "old", "state": "running", "finished": None})
    lock = state.acquire_lock(home, "new")
    assert lock.batch_id == "new"
    assert lock.stale_batch_id == "old"
    old = state.read_json(state.batch_path(home, "old"))
    assert old["state"] == "aborted"
    datetime.fromisoformat(old["finished"])
    assert state.current_lock(home)["batch_id"] == "new"


def test_stale_lock_without_a_batch_file_is_reclaimed(home, dead_pid):
    state.write_json(state.paths(home).lock, {"batch_id": "old", "pid": dead_pid, "started": "x"})
    lock = state.acquire_lock(home, "new")
    assert lock.stale_batch_id == "old"
    assert not state.batch_path(home, "old").exists()


def test_stale_lock_keeps_a_batch_that_already_finished(home, dead_pid):
    state.write_json(state.paths(home).lock, {"batch_id": "old", "pid": dead_pid, "started": "x"})
    done = {"batch_id": "old", "state": "done", "finished": "2026-01-01T00:00:00+00:00"}
    state.write_json(state.batch_path(home, "old"), done)
    lock = state.acquire_lock(home, "new")
    assert lock.stale_batch_id == "old"
    assert state.read_json(state.batch_path(home, "old")) == done


def test_unreadable_lock_is_reclaimed_without_a_stale_id(home):
    state.paths(home).lock.write_text("garbage", encoding="utf-8")
    lock = state.acquire_lock(home, "new")
    assert lock is not None
    assert lock.stale_batch_id is None


def test_write_lock_rewrites_the_pid_and_release_removes_it(home, dead_pid):
    state.write_lock(home, "a", os.getpid())
    raw = state.read_json(state.paths(home).lock)
    assert set(raw) == {"batch_id", "pid", "started"}
    assert raw["batch_id"] == "a"
    assert raw["pid"] == os.getpid()
    datetime.fromisoformat(raw["started"])
    assert state.current_lock(home)["pid"] == os.getpid()

    # Pointing the lock at a dead worker pid makes it stale: current_lock hides it.
    state.write_lock(home, "a", dead_pid)
    assert state.read_json(state.paths(home).lock)["pid"] == dead_pid
    assert state.current_lock(home) is None

    state.release_lock(home)
    assert not state.paths(home).lock.exists()
    state.release_lock(home)  # releasing a missing lock is not an error


def test_cancel_flag_round_trip(home):
    assert not state.is_cancelled(home, "a")
    state.request_cancel(home, "a")
    assert state.is_cancelled(home, "a")
    assert state.cancel_path(home, "a") == home / "batches" / "a.cancel"
    assert not state.is_cancelled(home, "b")


def test_paths_for_a_batch_live_under_batches_and_logs(home):
    assert state.batch_path(home, "a") == home / "batches" / "a.json"
    assert state.log_path(home, "a") == home / "logs" / "a.log"


def test_read_state_defaults_to_idle_when_the_file_is_missing(home):
    assert state.read_state(home) == {
        "state": "idle",
        "batch_id": None,
        "updated": None,
        "last_result": None,
    }


def test_set_state_writes_state_json_with_iso_time(home):
    state.set_state(home, "running", batch_id="x")
    written = state.read_json(state.paths(home).state)
    assert set(written) == {"state", "batch_id", "updated", "last_result"}
    assert written["state"] == "running"
    assert written["batch_id"] == "x"
    assert written["last_result"] is None
    assert datetime.fromisoformat(written["updated"]).tzinfo is not None
    assert state.read_state(home) == written

    state.set_state(home, "done", batch_id="x", last_result={"ok": True})
    assert state.read_state(home)["last_result"] == {"ok": True}

    # Without a new last_result the previous one is kept.
    state.set_state(home, "idle")
    after = state.read_state(home)
    assert after["state"] == "idle"
    assert after["batch_id"] is None
    assert after["last_result"] == {"ok": True}
