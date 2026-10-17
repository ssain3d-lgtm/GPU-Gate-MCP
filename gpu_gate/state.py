"""The state-directory contract.

The MCP server, the detached worker and any external UI talk to each other only through
files in one home directory:

    config.json               settings
    lock                      {"batch_id", "pid", "started"}; a live pid means a batch is running
    state.json                {"state", "batch_id", "updated", "last_result"}
    batches/<batch_id>.json   the batch file
    batches/<batch_id>.cancel existence means "cancel requested"
    logs/<batch_id>.log       worker log

Standard library only.
"""

from __future__ import annotations

import json
import os
import secrets
import sys
import tempfile
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from . import procs

ENV_HOME = "GPU_GATE_HOME"

# Batch states that mean the batch is over; a stale lock must not rewrite these.
FINISHED_STATES = ("done", "failed", "aborted")

_IDLE_STATE = {"state": "idle", "batch_id": None, "updated": None, "last_result": None}


@dataclass(frozen=True)
class Paths:
    config: Path
    lock: Path
    state: Path
    batches_dir: Path
    logs_dir: Path


@dataclass(frozen=True)
class Lock:
    batch_id: str
    stale_batch_id: str | None = None


def now_iso() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


def _default_home() -> Path:
    if sys.platform == "win32":
        base = os.environ.get("LOCALAPPDATA")
        root = Path(base) if base else Path.home() / "AppData" / "Local"
        return root / "gpu-gate"
    return Path.home() / ".local" / "share" / "gpu-gate"


def home() -> Path:
    """The state directory (GPU_GATE_HOME or the platform default), created if missing."""
    override = os.environ.get(ENV_HOME)
    root = Path(override) if override else _default_home()
    p = paths(root)
    p.batches_dir.mkdir(parents=True, exist_ok=True)
    p.logs_dir.mkdir(parents=True, exist_ok=True)
    return root


def paths(home: Path | str) -> Paths:
    root = Path(home)
    return Paths(
        config=root / "config.json",
        lock=root / "lock",
        state=root / "state.json",
        batches_dir=root / "batches",
        logs_dir=root / "logs",
    )


def read_json(path: Path | str) -> dict | None:
    """The JSON object in path, or None if it is missing, unreadable or not an object."""
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def write_json(path: Path | str, data: dict) -> None:
    """Atomically replace path with data: write a sibling temp file, then os.replace."""
    target = Path(path)
    text = json.dumps(data, ensure_ascii=False, indent=2)
    target.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=target.parent, prefix=target.name + ".", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(text)
        os.replace(tmp, target)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def new_batch_id() -> str:
    """YYYYMMDD-HHMMSS-<4 hex chars>, local time."""
    return f"{datetime.now():%Y%m%d-%H%M%S}-{secrets.token_hex(2)}"


def batch_path(home: Path | str, batch_id: str) -> Path:
    return paths(home).batches_dir / f"{batch_id}.json"


def cancel_path(home: Path | str, batch_id: str) -> Path:
    return paths(home).batches_dir / f"{batch_id}.cancel"


def log_path(home: Path | str, batch_id: str) -> Path:
    return paths(home).logs_dir / f"{batch_id}.log"


def write_lock(home: Path | str, batch_id: str, pid: int) -> None:
    write_json(paths(home).lock, {"batch_id": batch_id, "pid": pid, "started": now_iso()})


def current_lock(home: Path | str) -> dict | None:
    """The lock dict, but only while the pid in it is alive."""
    lock = read_json(paths(home).lock)
    if lock is None:
        return None
    pid = lock.get("pid")
    if isinstance(pid, int) and not isinstance(pid, bool) and procs.pid_alive(pid):
        return lock
    return None


def release_lock(home: Path | str) -> None:
    try:
        paths(home).lock.unlink()
    except FileNotFoundError:
        pass


def _abort_stale_batch(home: Path | str, batch_id: str) -> None:
    path = batch_path(home, batch_id)
    batch = read_json(path)
    if batch is None or batch.get("state") in FINISHED_STATES:
        return
    batch["state"] = "aborted"
    batch["finished"] = now_iso()
    write_json(path, batch)


def acquire_lock(home: Path | str, batch_id: str) -> Lock | None:
    """Take the lock for batch_id.

    Returns None while another batch is running (lock pid alive). A lock whose pid is dead is
    stale: its batch file is marked aborted, the lock is taken over, and the old id is reported
    as `stale_batch_id`.
    """
    stale_id = None
    existing = read_json(paths(home).lock)
    if existing is not None:
        if current_lock(home) is not None:
            return None
        old_id = existing.get("batch_id")
        if isinstance(old_id, str) and old_id:
            stale_id = old_id
            _abort_stale_batch(home, old_id)
    write_lock(home, batch_id, os.getpid())
    return Lock(batch_id=batch_id, stale_batch_id=stale_id)


def read_state(home: Path | str) -> dict:
    return read_json(paths(home).state) or dict(_IDLE_STATE)


def set_state(
    home: Path | str,
    state: str,
    batch_id: str | None = None,
    last_result: dict | None = None,
) -> None:
    """Write state.json; a None last_result keeps the previous one."""
    if last_result is None:
        last_result = read_state(home).get("last_result")
    write_json(
        paths(home).state,
        {"state": state, "batch_id": batch_id, "updated": now_iso(), "last_result": last_result},
    )


def is_cancelled(home: Path | str, batch_id: str) -> bool:
    return cancel_path(home, batch_id).exists()


def request_cancel(home: Path | str, batch_id: str) -> None:
    path = cancel_path(home, batch_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.touch()
