import json
import os
import subprocess
import sys
import time

import pytest

from gpu_gate import procs


def test_pid_alive_for_self_and_for_an_ended_child():
    assert procs.pid_alive(os.getpid())
    p = subprocess.Popen([sys.executable, "-c", "pass"])
    p.wait()
    assert not procs.pid_alive(p.pid)


def test_spawn_detached_writes_the_log_and_survives(tmp_path):
    log = tmp_path / "x.log"
    p = procs.spawn_detached([sys.executable, "-c", "print('hi')"], None, str(log))
    p.wait(10)
    assert log.read_text().strip() == "hi"


def test_kill_tree_ends_a_sleeping_child():
    p = procs.spawn_detached(
        [sys.executable, "-c", "import time; time.sleep(60)"], None, os.devnull
    )
    try:
        assert procs.kill_tree(p.pid)
        p.wait(10)
        assert not procs.pid_alive(p.pid)
    finally:
        if p.poll() is None:
            p.kill()
            p.wait(10)


def test_listener_pids_parses_netstat_text(monkeypatch):
    text = (
        "  TCP    127.0.0.1:8188    0.0.0.0:0    LISTENING    4242\n"
        "  TCP    0.0.0.0:81880   0.0.0.0:0    LISTENING    1\n"
    )
    monkeypatch.setattr(procs, "_netstat", lambda: text)
    assert procs.listener_pids(8188) == [4242]


def test_listener_pids_parses_ss_text_and_keeps_unique_order(monkeypatch):
    text = (
        "State  Recv-Q Send-Q Local Address:Port Peer Address:Port Process\n"
        'LISTEN 0      128        127.0.0.1:8188      0.0.0.0:*     users:(("python",pid=77,fd=3),("python",pid=78,fd=4))\n'
        'LISTEN 0      128             [::]:8188         [::]:*     users:(("python",pid=77,fd=5))\n'
        'LISTEN 0      128          0.0.0.0:9999      0.0.0.0:*     users:(("other",pid=5,fd=3))\n'
    )
    monkeypatch.setattr(procs, "_netstat", lambda: text)
    assert procs.listener_pids(8188) == [77, 78]


def test_listener_pids_returns_empty_when_the_listing_fails(monkeypatch):
    monkeypatch.setattr(procs, "_netstat", lambda: "")
    assert procs.listener_pids(8188) == []


def test_listener_pids_finds_this_process_on_a_real_port():
    import socket

    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        s.listen()
        port = s.getsockname()[1]
        assert os.getpid() in procs.listener_pids(port)


def test_kill_tree_also_ends_grandchildren(tmp_path):
    code = (
        "import subprocess, sys, time\n"
        "g = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)'])\n"
        "print(g.pid, flush=True)\n"
        "time.sleep(60)\n"
    )
    log = tmp_path / "tree.log"
    parent = procs.spawn_detached([sys.executable, "-c", code], None, str(log))
    grandchild = 0
    try:
        for _ in range(100):
            text = log.read_text().strip()
            if text:
                grandchild = int(text)
                break
            time.sleep(0.1)
        assert grandchild and procs.pid_alive(grandchild)
        assert procs.kill_tree(parent.pid)
        parent.wait(10)
        for _ in range(50):
            if not procs.pid_alive(grandchild):
                break
            time.sleep(0.1)
        assert not procs.pid_alive(grandchild)
    finally:
        if parent.poll() is None:
            parent.kill()
            parent.wait(10)
        if grandchild:
            procs.kill_tree(grandchild)


def test_stop_pids_ends_every_process_and_says_so():
    kids = [
        procs.spawn_detached([sys.executable, "-c", "import time; time.sleep(60)"], None, os.devnull)
        for _ in range(2)
    ]
    try:
        assert procs.stop_pids([k.pid for k in kids]) is True
        for kid in kids:
            kid.wait(10)
            assert not procs.pid_alive(kid.pid)
    finally:
        for kid in kids:
            if kid.poll() is None:
                kid.kill()
                kid.wait(10)


def test_stop_pids_reports_false_for_a_pid_that_will_not_die(monkeypatch):
    killed = []
    monkeypatch.setattr(procs, "kill_tree", lambda pid: killed.append(pid) or pid == 2)
    monkeypatch.setattr(procs, "pid_alive", lambda pid: False)  # POSIX: nothing to wait for
    monkeypatch.setattr(os, "kill", lambda pid, sig: None)
    assert procs.stop_pids([1, 2]) is False
    assert killed == [1, 2]  # the second is still tried after the first fails


def test_process_name_reads_tasklist_csv_and_drops_exe(monkeypatch):
    seen = []
    monkeypatch.setattr(procs, "IS_WINDOWS", True)
    monkeypatch.setattr(
        procs,
        "_run_quiet",
        lambda argv, timeout=30: seen.append(argv) or '"python.exe","12356","Console","1","1,234 K"\n',
    )
    assert procs.process_name(12356) == "python"
    assert seen == [["tasklist", "/FO", "CSV", "/NH", "/FI", "PID eq 12356"]]


def test_process_name_is_none_when_tasklist_finds_nothing(monkeypatch):
    monkeypatch.setattr(procs, "IS_WINDOWS", True)
    for text in ("", "INFO: No tasks are running which match the specified criteria.\r\n"):
        monkeypatch.setattr(procs, "_run_quiet", lambda argv, timeout=30, text=text: text)
        assert procs.process_name(1) is None


def test_process_name_reads_proc_comm_off_windows(monkeypatch, tmp_path):
    (tmp_path / "42.comm").write_text("python3\n")
    monkeypatch.setattr(procs, "IS_WINDOWS", False)
    monkeypatch.setattr(procs, "_COMM_PATH", str(tmp_path / "{pid}.comm"))
    assert procs.process_name(42) == "python3"
    assert procs.process_name(43) is None


# --- the process table: find_pids / descendants ------------------------------------------------


def _windows_table(monkeypatch, text):
    """IS_WINDOWS on, _run_quiet answering text; the argv lists it was called with."""
    seen = []
    monkeypatch.setattr(procs, "IS_WINDOWS", True)
    monkeypatch.setattr(procs, "_run_quiet", lambda argv, timeout=30, **kw: seen.append(argv) or text)
    return seen


def _cim(pid, ppid, command_line, exe=None):
    return {"ProcessId": pid, "ParentProcessId": ppid, "CommandLine": command_line, "ExecutablePath": exe}


def test_find_pids_reads_a_single_cim_object(monkeypatch):
    # ConvertTo-Json writes one object, not a list, when only one process is listed. The bat starts
    # ComfyUI with relative paths: the install folder shows only in the executable path.
    text = json.dumps(_cim(
        10720, 18968,
        r".\python_embeded\python.exe -I -W ignore::FutureWarning ComfyUI\main.py --windows-standalone-build",
        r"X:\Tools\Comfy Install\python_embeded\python.exe",
    ))
    seen = _windows_table(monkeypatch, text)
    assert procs.find_pids(["MAIN.PY", "x:/tools/COMFY install"]) == [10720]
    assert procs.find_pids(["main.py", r"X:\Tools\Comfy Install"]) == [10720]  # either slash
    assert procs.find_pids(["main.py", "x:/tools/other"]) == []
    argv = seen[0]
    assert argv[:4] == ["powershell", "-NoProfile", "-NonInteractive", "-Command"]
    assert "Get-CimInstance Win32_Process" in argv[4] and "ConvertTo-Json -Compress" in argv[4]


def test_find_pids_reads_a_cim_list_and_skips_itself_and_its_parent(monkeypatch):
    me, parent = os.getpid(), os.getppid()
    text = json.dumps([
        _cim(4, 0, None),  # System: no command line
        _cim(me, parent, "python -m pytest backend.main uvicorn"),
        _cim(parent, 1, "uvicorn backend.main --port 3900"),
        _cim(500, 1, r"X:\venv\Scripts\python.exe -m UVICORN Backend.Main:app --port 3900", r"X:\venv\Scripts\python.exe"),
        _cim(501, 500, r"X:\venv\Scripts\python.exe -m uvicorn other.app"),
        _cim(502, 1, "uvicorn backend.main:app --reload"),
    ])
    _windows_table(monkeypatch, text)
    assert procs.find_pids(["uvicorn", "backend.main"]) == [500, 502]


def test_find_pids_is_empty_when_the_listing_fails(monkeypatch):
    for text in ("", "not json", "[1, 2]", '{"ProcessId": "x"}'):
        _windows_table(monkeypatch, text)
        assert procs.find_pids(["main.py"]) == []


def test_find_pids_reads_ps_off_windows(monkeypatch):
    seen = []
    text = (
        "    1     0 /sbin/init\n"
        "  300     1 /opt/comfy/venv/bin/python /opt/comfy/main.py --listen\n"
        "  301   300 /opt/comfy/venv/bin/python -c pass\n"
        "garbage line\n"
    )
    monkeypatch.setattr(procs, "IS_WINDOWS", False)
    monkeypatch.setattr(procs, "_run_quiet", lambda argv, timeout=30, **kw: seen.append(argv) or text)
    assert procs.find_pids(["main.py", "/OPT/comfy"]) == [300]
    assert seen[0][0] == "ps"
    assert procs.descendants(1) == [300, 301]


def test_descendants_walks_the_whole_tree(monkeypatch):
    text = json.dumps([
        _cim(100, 1, "cmd"), _cim(200, 100, "a"), _cim(201, 100, "b"),
        _cim(300, 200, "c"), _cim(400, 999, "d"), _cim(1, 0, "init"),
    ])
    _windows_table(monkeypatch, text)
    assert sorted(procs.descendants(100)) == [200, 201, 300]
    assert procs.descendants(400) == []


def test_descendants_raises_when_the_listing_fails(monkeypatch):
    # An empty answer would read as "no children"; the caller must be able to tell it apart.
    _windows_table(monkeypatch, "")
    with pytest.raises(OSError):
        procs.descendants(100)
