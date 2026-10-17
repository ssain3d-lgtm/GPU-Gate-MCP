"""Process helpers: detached spawn, pid liveness, killing a process tree, stopping pids, port listeners,
the process table (find_pids by command line, descendants).

Standard library only. Windows is the primary platform (ctypes + taskkill/netstat);
the POSIX branches are deliberately minimal.
"""

from __future__ import annotations

import csv
import functools
import io
import json
import os
import re
import signal
import subprocess
import sys
import time
from collections.abc import Callable
from urllib.parse import urlsplit

IS_WINDOWS = sys.platform == "win32"

# Windows process creation flags.
CREATE_NEW_PROCESS_GROUP = 0x00000200
CREATE_NO_WINDOW = 0x08000000
CREATE_BREAKAWAY_FROM_JOB = 0x01000000

# Windows process access / state values.
PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
STILL_ACTIVE = 259
ERROR_ACCESS_DENIED = 5

_KILL_WAIT_SECONDS = 3.0
STOP_WAIT_ROUNDS = 20  # stop_pids gives a terminated process 20 x 0.5 s = 10 s to exit
STOP_POLL_S = 0.5

_COMM_PATH = "/proc/{pid}/comm"


def _run_quiet(argv: list[str], timeout: float = 30, encoding: str | None = None) -> str:
    """Run a command without a console window; return its stdout text (decoded with encoding, else
    the locale's), or "" on any failure."""
    kwargs: dict = {}
    if IS_WINDOWS:
        kwargs["creationflags"] = CREATE_NO_WINDOW
    try:
        done = subprocess.run(
            argv,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
            encoding=encoding,
            errors="replace",
            timeout=timeout,
            **kwargs,
        )
    except (OSError, subprocess.SubprocessError):
        return ""
    return done.stdout or ""


def spawn_detached(
    argv: list[str],
    cwd: str | None,
    log_path: str,
    env: dict | None = None,
) -> subprocess.Popen:
    """Start argv in its own process group/session, stdout+stderr appended to log_path."""
    options: dict = {
        "cwd": cwd,
        "env": env,
        "stdin": subprocess.DEVNULL,
        "close_fds": True,
    }
    with open(log_path, "ab") as log:
        options["stdout"] = log
        options["stderr"] = subprocess.STDOUT
        if not IS_WINDOWS:
            return subprocess.Popen(argv, start_new_session=True, **options)
        flags = CREATE_NEW_PROCESS_GROUP | CREATE_NO_WINDOW
        try:
            return subprocess.Popen(
                argv, creationflags=flags | CREATE_BREAKAWAY_FROM_JOB, **options
            )
        except OSError:
            # The parent's job object may forbid breakaway; run inside it instead.
            return subprocess.Popen(argv, creationflags=flags, **options)


@functools.lru_cache(maxsize=1)
def _kernel32():
    import ctypes
    from ctypes import wintypes

    k32 = ctypes.WinDLL("kernel32", use_last_error=True)
    k32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    k32.OpenProcess.restype = wintypes.HANDLE
    k32.GetExitCodeProcess.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)]
    k32.GetExitCodeProcess.restype = wintypes.BOOL
    k32.CloseHandle.argtypes = [wintypes.HANDLE]
    k32.CloseHandle.restype = wintypes.BOOL
    return k32


def _pid_alive_windows(pid: int) -> bool:
    import ctypes
    from ctypes import wintypes

    k32 = _kernel32()
    handle = k32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
    if not handle:
        # Access denied means the process exists but is protected from us.
        return ctypes.get_last_error() == ERROR_ACCESS_DENIED
    try:
        code = wintypes.DWORD()
        if not k32.GetExitCodeProcess(handle, ctypes.byref(code)):
            return False
        return code.value == STILL_ACTIVE
    finally:
        k32.CloseHandle(handle)


def pid_alive(pid: int) -> bool:
    """True if a process with this pid is currently running."""
    if pid <= 0:
        return False
    if IS_WINDOWS:
        return _pid_alive_windows(pid)
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return False
    return True


def process_name(pid: int) -> str | None:
    """The image name of a running process ("python" for python.exe), or None when unknown."""
    if pid <= 0:
        return None
    if not IS_WINDOWS:
        try:
            with open(_COMM_PATH.format(pid=pid), encoding="utf-8", errors="replace") as comm:
                return comm.read().strip() or None
        except OSError:
            return None
    text = _run_quiet(["tasklist", "/FO", "CSV", "/NH", "/FI", f"PID eq {pid}"])
    try:
        for row in csv.reader(io.StringIO(text)):
            # "python.exe","12356","Console","1","1,234 K"; a miss prints one INFO sentence instead
            if len(row) >= 2 and row[1] == str(pid) and row[0]:
                name = row[0]
                return name[:-4] if name.lower().endswith(".exe") else name
    except csv.Error:
        pass
    return None


def kill_tree(pid: int) -> bool:
    """Kill pid and all of its descendants; True if the process is gone afterwards."""
    if pid <= 0:
        return False
    if IS_WINDOWS:
        _run_quiet(["taskkill", "/PID", str(pid), "/T", "/F"])
        deadline = time.monotonic() + _KILL_WAIT_SECONDS
        while pid_alive(pid) and time.monotonic() < deadline:
            time.sleep(0.05)
        return not pid_alive(pid)

    delivered = False
    for send in (os.killpg, os.kill):
        try:
            send(pid, signal.SIGKILL)
            delivered = True
        except OSError:
            pass
    return delivered or not pid_alive(pid)


def _terminate(pid: int, proc: subprocess.Popen | None) -> None:
    try:
        if proc is not None and proc.pid == pid:
            proc.terminate()
        else:
            os.kill(pid, signal.SIGTERM)
    except OSError:
        pass  # already gone, or not ours to end: kill_tree settles it


def _alive(pid: int, proc: subprocess.Popen | None) -> bool:
    if proc is not None and proc.pid == pid:
        return proc.poll() is None  # also reaps the child, which pid_alive would still see
    return pid_alive(pid)


def stop_pids(
    pids: list[int],
    sleep: Callable[[float], None] | None = None,
    proc: subprocess.Popen | None = None,
) -> bool:
    """End each pid and everything it started; True when kill_tree reports every one gone.

    On Windows terminating is not graceful (TerminateProcess), and the parent dying first would
    orphan its children, so the process tree is killed at once. Elsewhere each pid is sent SIGTERM,
    all of them get up to 10 s (20 polls of `sleep(0.5)`) to exit, and the trees are killed after
    that. kill_tree always runs for every pid (it is True for one that is already gone).

    proc, when given, is the Popen of one of the pids: it is terminated and polled through the
    Popen object (a child that exited is a zombie that pid_alive still sees) and its exit status is
    collected at the end, so no zombie or ResourceWarning is left behind. `sleep` defaults to
    time.sleep, looked up on each call so a patched time.sleep is honoured.
    """
    sleep = sleep or time.sleep
    if not IS_WINDOWS:
        for pid in pids:
            _terminate(pid, proc)
        for _ in range(STOP_WAIT_ROUNDS):
            if not any(_alive(pid, proc) for pid in pids):
                break
            sleep(STOP_POLL_S)
    gone = [bool(kill_tree(pid)) for pid in pids]  # every pid, even after one fails
    if proc is not None:
        proc.poll()  # collect the exit status
    return all(gone)


def _netstat() -> str:
    """Raw listening-socket listing: `netstat -ano` on Windows, `ss -ltnp` elsewhere."""
    if IS_WINDOWS:
        return _run_quiet(["netstat", "-ano"])
    return _run_quiet(["ss", "-ltnp"])


_SS_PID = re.compile(r"pid=(\d+)")


def listener_pids(port: int) -> list[int]:
    """Unique pids listening on TCP `port`, in order of appearance; [] on any failure."""
    try:
        text = _netstat()
    except Exception:
        return []
    suffix = f":{port}"
    found: list[int] = []
    for line in text.splitlines():
        cols = line.split()
        if len(cols) >= 5 and cols[0].upper().startswith("TCP") and cols[3] == "LISTENING":
            # netstat -ano:  TCP  <local>  <remote>  LISTENING  <pid>
            if cols[1].endswith(suffix) and cols[-1].isdigit():
                pids = [int(cols[-1])]
            else:
                continue
        elif len(cols) >= 4 and cols[0] == "LISTEN":
            # ss -ltnp:  LISTEN  <recv-q>  <send-q>  <local>  <peer>  users:(("x",pid=N,fd=M))
            if cols[3].endswith(suffix):
                pids = [int(m) for m in _SS_PID.findall(line)]
            else:
                continue
        else:
            continue
        for found_pid in pids:
            if found_pid not in found:
                found.append(found_pid)
    return found


# Every process with its parent, command line and executable path, as JSON (UTF-8, so paths
# outside the ANSI code page survive). ConvertTo-Json writes a lone object, not a list, for one row.
_CIM_QUERY = (
    "[Console]::OutputEncoding = [System.Text.Encoding]::UTF8; "
    "Get-CimInstance Win32_Process | Select-Object ProcessId, ParentProcessId, CommandLine, ExecutablePath "
    "| ConvertTo-Json -Compress"
)
_TABLE_TIMEOUT_S = 60


def _parse_cim(text: str) -> list[tuple[int, int, str]]:
    try:
        rows = json.loads(text)
    except ValueError:
        return []
    if isinstance(rows, dict):
        rows = [rows]
    if not isinstance(rows, list):
        return []
    table = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        pid, ppid = row.get("ProcessId"), row.get("ParentProcessId")
        if not isinstance(pid, int) or isinstance(pid, bool):
            continue
        words = " ".join(str(row.get(key) or "") for key in ("CommandLine", "ExecutablePath"))
        table.append((pid, ppid if isinstance(ppid, int) else 0, words))
    return table


def _parse_ps(text: str) -> list[tuple[int, int, str]]:
    table = []
    for line in text.splitlines():
        cols = line.split(None, 2)
        if len(cols) >= 2 and cols[0].isdigit() and cols[1].isdigit():
            table.append((int(cols[0]), int(cols[1]), cols[2] if len(cols) > 2 else ""))
    return table


def _process_table() -> list[tuple[int, int, str]]:
    """(pid, parent pid, command line and executable path) of every process; [] on any failure.

    Windows: one Win32_Process query through PowerShell. Elsewhere: `ps -eo pid=,ppid=,args=`.
    """
    try:
        if IS_WINDOWS:
            argv = ["powershell", "-NoProfile", "-NonInteractive", "-Command", _CIM_QUERY]
            return _parse_cim(_run_quiet(argv, timeout=_TABLE_TIMEOUT_S, encoding="utf-8"))
        return _parse_ps(_run_quiet(["ps", "-eo", "pid=,ppid=,args="], timeout=_TABLE_TIMEOUT_S))
    except Exception:
        return []


def _fold(text: str) -> str:
    """Lower case with forward slashes, so a path matches however its separators are written."""
    return text.replace("\\", "/").lower()


def find_pids(substrings: list[str]) -> list[int]:
    """Pids of the processes whose command line contains every one of substrings, ignoring case and
    `\\` versus `/`; [] on any failure. This process and its parent are never listed.

    On Windows the executable path counts as part of the command line: a program started with a
    relative path (`.\\python_embeded\\python.exe ComfyUI\\main.py`) shows its folder only there.
    """
    wanted = [_fold(s) for s in substrings]
    skip = {os.getpid(), os.getppid()}
    return [
        pid for pid, _, words in _process_table()
        if pid not in skip and all(w in _fold(words) for w in wanted)
    ]


def descendants(pid: int) -> list[int]:
    """Every process started by pid, its children's children and so on (pid itself not included).

    Raises OSError when the process list cannot be read: an empty list would mean "no children",
    which a caller deciding whose process something is must not be told by mistake.
    """
    table = _process_table()
    if not table:
        raise OSError("the process list could not be read")
    children: dict[int, list[int]] = {}
    for child, parent, _ in table:
        if child != parent:
            children.setdefault(parent, []).append(child)
    found: list[int] = []
    seen, todo = {pid}, [pid]
    while todo:
        for child in children.get(todo.pop(), []):
            if child not in seen:
                seen.add(child)
                found.append(child)
                todo.append(child)
    return found


def url_port(url: str) -> int:
    """The TCP port a URL points at: the explicit one, else 443 for https and 80 for the rest."""
    parts = urlsplit(url)
    return parts.port or (443 if parts.scheme == "https" else 80)
