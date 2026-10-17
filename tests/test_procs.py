import os
import subprocess
import sys
import time

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
