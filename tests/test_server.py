import io
import itertools
import json
import os
import subprocess
import sys
import threading
import time

import pytest

from fakes import FakeStrata
from gpu_gate import config, state
from gpu_gate.config import ComfyCfg, Config, Limits, StrataCfg, VoiceCfg
from gpu_gate.server import Server, worker_argv

_ids = itertools.count(1)


def rpc(method, params=None, id=None):
    msg = {"jsonrpc": "2.0", "id": next(_ids) if id is None else id, "method": method}
    if params is not None:
        msg["params"] = params
    return msg


def call(server, name, arguments=None, id=None):
    reply = server.handle(rpc("tools/call", {"name": name, "arguments": arguments or {}}, id))
    return reply["result"]


JOBS = [{"kind": "workflow", "path": "/tmp/a.json"}]


def finish_batch(path, status="done"):
    """What the real worker leaves behind: a result, a final state and `finished`."""
    doc = state.read_json(path)
    result = {
        "batch_id": doc["batch_id"],
        "ok": status == "done",
        "jobs": [{"index": i, "status": status} for i, _ in enumerate(doc["jobs"])],
        "warnings": list(doc["warnings"]),
        "error": None,
    }
    doc.update(state="done" if status == "done" else "failed", result=result, finished=state.now_iso())
    state.write_json(path, doc)


class FakeSpawn:
    """Stands in for the detached worker. mode "finish": the batch is finished before it returns;
    "dead": nothing is written and the returned pid is not running; "live": nothing is written."""

    def __init__(self, mode="finish", pid=None):
        self.mode = mode
        self.pid = pid
        self.paths = []
        self.seen = None  # the batch file as the worker would have read it
        self.spawned = threading.Event()

    def __call__(self, batch_path):
        self.paths.append(str(batch_path))
        self.seen = state.read_json(batch_path)
        if self.mode == "finish":
            finish_batch(batch_path)
        self.spawned.set()
        return self.pid if self.pid is not None else os.getpid()


@pytest.fixture
def home(tmp_path, monkeypatch):
    monkeypatch.setenv(state.ENV_HOME, str(tmp_path))
    monkeypatch.delenv(config.ENV_CONFIG, raising=False)
    return state.home()


@pytest.fixture
def dead_pid():
    p = subprocess.Popen([sys.executable, "-c", "pass"])
    p.wait()
    return p.pid


def make_cfg(tmp_path, strata_cfg=None, comfy_cfg=None, voice_cfg=None):
    return Config(
        strata=strata_cfg or StrataCfg(url=FakeStrata.DEAD_URL),
        comfyui=comfy_cfg or ComfyCfg(dir=str(tmp_path), bat="Start ComfyUI.bat"),
        voicestudio=voice_cfg or VoiceCfg(),
        limits=Limits(),
        other_llm_ports=[],
        path=str(tmp_path / "config.json"),
    )


@pytest.fixture
def spawn():
    return FakeSpawn()


@pytest.fixture
def server(home, tmp_path, fake_strata, spawn):
    _, strata_cfg = fake_strata
    cfg = make_cfg(tmp_path, strata_cfg)
    return Server(home, lambda: cfg, spawn, poll_interval=0.01)


@pytest.fixture
def server_without_config(home, spawn):
    return Server(home, lambda: config.load(home), spawn, poll_interval=0.01)


def test_initialize_and_tools_list(server):
    r = server.handle(rpc("initialize", {"protocolVersion": "2025-03-26"}))
    result = r["result"]
    assert result["protocolVersion"] == "2025-03-26"
    assert result["capabilities"] == {"tools": {"listChanged": False}}
    assert result["serverInfo"]["name"] == "gpu-gate"
    assert "comfy_batch" in result["instructions"]
    unknown = server.handle(rpc("initialize", {"protocolVersion": "1999-01-01"}))
    assert unknown["result"]["protocolVersion"] == "2025-06-18"

    tools = server.handle(rpc("tools/list"))["result"]["tools"]
    assert [t["name"] for t in tools] == ["comfy_batch", "voice_batch", "gate_status"]
    status = tools[2]
    assert status["annotations"]["readOnlyHint"] is True
    assert server.handle(rpc("ping"))["result"] == {}
    assert server.handle(rpc("resources/list"))["result"] == {"resources": []}
    assert server.handle(rpc("prompts/list"))["result"] == {"prompts": []}
    assert server.handle(rpc("no/such"))["error"]["code"] == -32601
    assert server.handle({"jsonrpc": "2.0", "method": "notifications/initialized"}) is None


def test_comfy_batch_writes_batch_file_spawns_worker_and_returns_result(server, home, spawn):
    res = call(server, "comfy_batch", {"jobs": JOBS, "options": {"stop_on_error": True}})

    assert res["isError"] is False
    assert len(spawn.paths) == 1
    seen = spawn.seen
    batch_id = seen["batch_id"]
    assert spawn.paths[0] == str(state.batch_path(home, batch_id))
    assert seen["kind"] == "comfyui"
    assert seen["state"] == "pending"
    assert seen["jobs"] == JOBS
    assert seen["options"] == {"stop_on_error": True}
    assert seen["warnings"] == []
    assert seen["result"] is None and seen["finished"] is None
    assert seen["started"]

    written = state.read_json(state.batch_path(home, batch_id))
    assert res["structuredContent"] == written["result"]
    assert json.loads(res["content"][0]["text"]) == written["result"]
    assert not state.paths(home).lock.exists()  # the worker's lock is cleaned up


def test_second_call_while_locked_is_busy(server, home, spawn):
    state.acquire_lock(home, "x")
    res = call(server, "comfy_batch", {"jobs": JOBS})
    assert res["isError"] is True
    assert res["structuredContent"]["error"] == "busy"
    assert res["structuredContent"]["batch"]["batch_id"] == "x"
    assert spawn.paths == []


def test_aborted_batch_is_reported_and_strata_reloaded(home, tmp_path, spawn, fake_strata_unloaded, dead_pid):
    fake, strata_cfg = fake_strata_unloaded
    cfg = make_cfg(tmp_path, strata_cfg)
    server = Server(home, lambda: cfg, spawn, poll_interval=0.01)
    old = state.batch_path(home, "old")
    state.write_json(old, {"batch_id": "old", "kind": "comfyui", "jobs": JOBS, "state": "running"})
    state.write_json(state.paths(home).lock, {"batch_id": "old", "pid": dead_pid, "started": "x"})

    res = call(server, "comfy_batch", {"jobs": JOBS})

    assert res["isError"] is False
    assert any("aborted" in w and "old" in w for w in res["structuredContent"]["warnings"])
    assert state.read_json(old)["state"] == "aborted"
    assert fake.loaded is True
    assert ("POST", "/load") in fake.calls


def test_tool_error_is_one_line(server_without_config, home):
    res = call(server_without_config, "comfy_batch", {"jobs": JOBS})
    assert res["isError"] is True
    error = res["structuredContent"]["error"]
    assert "config not found" in error
    assert str(config.config_path(home)) in error
    assert "\n" not in error
    assert not state.paths(home).lock.exists()


def test_cancelled_notification_sets_the_flag(home, tmp_path, fake_strata):
    _, strata_cfg = fake_strata
    cfg = make_cfg(tmp_path, strata_cfg)
    spawn = FakeSpawn(mode="live")
    polls = []

    def sleep(seconds):
        # The slow worker: it finishes once the cancel flag is there (or gives up after 5 s).
        path = spawn.paths[0]
        batch_id = state.read_json(path)["batch_id"]
        polls.append(seconds)
        if state.is_cancelled(home, batch_id) or len(polls) > 500:
            finish_batch(path, status="cancelled")
        else:
            time.sleep(0.01)

    server = Server(home, lambda: cfg, spawn, poll_interval=0.01, sleep=sleep)
    replies = []
    t = threading.Thread(target=lambda: replies.append(server.handle(rpc(
        "tools/call", {"name": "comfy_batch", "arguments": {"jobs": JOBS}}, id=7))))
    t.start()
    assert spawn.spawned.wait(5)
    batch_id = spawn.seen["batch_id"]

    # Only the request this process is running can be cancelled.
    assert server.handle({"jsonrpc": "2.0", "method": "notifications/cancelled",
                          "params": {"requestId": 8}}) is None
    assert not state.is_cancelled(home, batch_id)
    assert server.handle({"jsonrpc": "2.0", "method": "notifications/cancelled",
                          "params": {"requestId": 7, "reason": "user"}}) is None
    assert state.cancel_path(home, batch_id).exists()

    t.join(5)
    assert not t.is_alive()
    assert len(polls) <= 500
    assert replies[0]["result"]["structuredContent"]["jobs"][0]["status"] == "cancelled"


def test_worker_death_is_reported(server, home, spawn, dead_pid):
    spawn.mode, spawn.pid = "dead", dead_pid
    res = call(server, "comfy_batch", {"jobs": JOBS})
    assert res["isError"] is True
    error = res["structuredContent"]["error"]
    assert "worker died" in error
    batch_id = spawn.seen["batch_id"]
    assert str(state.log_path(home, batch_id)) in error
    doc = state.read_json(state.batch_path(home, batch_id))
    assert doc["state"] == "aborted" and doc["finished"]
    assert not state.paths(home).lock.exists()


def test_gate_status_shape(home, tmp_path, spawn, fake_strata, fake_comfy, voice_factory):
    _, strata_cfg = fake_strata
    _, comfy_cfg = fake_comfy
    _, voice_cfg = voice_factory(status="starting")
    cfg = make_cfg(tmp_path, strata_cfg, comfy_cfg, voice_cfg)
    gpu = [
        {"pid": 11, "name": "strata-server", "vram_mb": 20000},
        {"pid": 12, "name": "python", "vram_mb": 5000},
        {"pid": 13, "name": "dwm", "vram_mb": 1200},
    ]
    server = Server(home, lambda: cfg, spawn, poll_interval=0.01, process_vram=lambda: gpu)

    res = call(server, "gate_status")

    assert res["isError"] is False
    status = res["structuredContent"]
    assert set(status) == {"state", "batch", "strata", "executor", "gpu", "warnings", "last_result"}
    assert status["state"] == "idle"
    assert status["batch"] is None
    assert status["strata"] == {"server": True, "loaded": True, "busy": False}
    assert status["executor"] == {"comfyui": "up(external)", "voicestudio": "down"}
    assert status["gpu"] == gpu
    assert status["warnings"] == ["python(pid 12) holds 5000 MB while the LLM is loaded"]
    assert status["last_result"] is None


def test_gate_status_reports_a_dead_worker_and_a_missing_counter(home, tmp_path, spawn, fake_strata, dead_pid):
    _, strata_cfg = fake_strata
    cfg = make_cfg(tmp_path, strata_cfg, ComfyCfg(url=strata_cfg.url), VoiceCfg(url=strata_cfg.url))
    server = Server(home, lambda: cfg, spawn, poll_interval=0.01, process_vram=lambda: None)
    state.write_json(state.paths(home).lock, {"batch_id": "old", "pid": dead_pid, "started": "x"})

    status = call(server, "gate_status")["structuredContent"]

    assert status["state"] == "aborted"
    assert status["gpu"] is None
    assert any("old" in w and "aborted" in w for w in status["warnings"])
    assert "GPU process memory unavailable" in status["warnings"]


def test_jobs_schema_is_enforced(server, spawn):
    res = call(server, "comfy_batch", {"jobs": []})
    assert res["isError"] is True
    assert "jobs" in res["structuredContent"]["error"]
    res = call(server, "voice_batch", {"jobs": [{"text": "hi", "out": "a.wav"}], "bogus": 1})
    assert res["isError"] is True
    assert "bogus" in res["structuredContent"]["error"]
    res = call(server, "comfy_batch", {})
    assert res["isError"] is True and "jobs" in res["structuredContent"]["error"]
    assert spawn.paths == []


def test_stdout_carries_only_protocol(server, capsys):
    lines = [
        rpc("initialize", {"protocolVersion": "2025-06-18"}, id=1),
        {"jsonrpc": "2.0", "method": "notifications/initialized"},
        rpc("tools/call", {"name": "comfy_batch", "arguments": {"jobs": JOBS}}, id=2),
        rpc("tools/list", id=3),
    ]
    inp = io.BytesIO(b"".join(json.dumps(m).encode("utf-8") + b"\n" for m in lines) + b"not json\n")
    out = io.BytesIO()

    server.serve(inp, out)

    replies = [json.loads(line) for line in out.getvalue().decode("utf-8").splitlines()]
    assert len(replies) == 4  # three requests and the parse error; the notification gets none
    assert sorted(r["id"] for r in replies if r["id"] is not None) == [1, 2, 3]
    assert [r["error"]["code"] for r in replies if r["id"] is None] == [-32700]
    assert capsys.readouterr().out == ""


def test_serve_returns_promptly_when_stdin_closes_during_a_batch(home, tmp_path, fake_strata, capsys):
    _, strata_cfg = fake_strata
    cfg = make_cfg(tmp_path, strata_cfg)
    spawn = FakeSpawn(mode="live")  # a worker that is alive and never finishes on its own
    server = Server(home, lambda: cfg, spawn, poll_interval=0.01)
    line = json.dumps(rpc("tools/call", {"name": "comfy_batch", "arguments": {"jobs": JOBS}}, id=5))

    def stdin():
        yield line.encode("utf-8") + b"\n"
        assert spawn.spawned.wait(5)  # EOF comes once the batch is running

    before = set(threading.enumerate())
    t0 = time.monotonic()
    server.serve(stdin(), io.BytesIO())
    elapsed = time.monotonic() - t0

    assert elapsed < 2
    assert state.paths(home).lock.exists()  # the batch is still considered running
    batch_id = spawn.seen["batch_id"]
    assert f"stdin closed; batch {batch_id} continues in worker pid {os.getpid()}" in capsys.readouterr().err

    # Let the call end so no thread outlives the test.
    finish_batch(spawn.paths[0])
    for thread in set(threading.enumerate()) - before:
        thread.join(5)
        assert not thread.is_alive()


JOBS3 = [{"kind": "workflow", "path": f"/tmp/{name}.json"} for name in ("a", "b", "c")]


def serve_a_batch(home, tmp_path, fake_strata, meta=None, polls=3):
    """Serve one comfy_batch call whose (fake) worker moves one job a poll and finishes after
    `polls` polls; the stdout messages in order."""
    _, strata_cfg = fake_strata
    cfg = make_cfg(tmp_path, strata_cfg)
    spawn = FakeSpawn(mode="live")
    finished = threading.Event()
    seen = []

    def sleep(seconds):
        seen.append(seconds)
        path = spawn.paths[0]
        doc = state.read_json(path)
        done = len(seen)
        doc.update(state="running", progress={"done": done, "total": 3, "current": f"job {done}"})
        state.write_json(path, doc)
        if done >= polls:
            finish_batch(path)
            finished.set()

    server = Server(home, lambda: cfg, spawn, poll_interval=0.01, sleep=sleep)
    params = {"name": "comfy_batch", "arguments": {"jobs": JOBS3}}
    if meta is not None:
        params["_meta"] = meta
    line = json.dumps(rpc("tools/call", params, id=11)).encode("utf-8") + b"\n"

    def stdin():
        yield line
        assert finished.wait(5)

    out = io.BytesIO()
    server.serve(stdin(), out)
    return [json.loads(raw) for raw in out.getvalue().decode("utf-8").splitlines()]


@pytest.mark.parametrize("token", ["tok-1", 42])
def test_progress_notifications_carry_the_token_until_the_result(home, tmp_path, fake_strata, token):
    messages = serve_a_batch(home, tmp_path, fake_strata, meta={"progressToken": token})
    reply = next(i for i, m in enumerate(messages) if m.get("id") == 11)
    assert reply == len(messages) - 1  # the result comes last
    progress = [m for m in messages[:reply] if m.get("method") == "notifications/progress"]
    assert len(progress) >= 3 and len(progress) == reply
    for m in progress:
        assert m["jsonrpc"] == "2.0" and "id" not in m
        assert m["params"]["progressToken"] == token
        assert m["params"]["total"] == 3
        assert len(m["params"]["message"]) <= 120
    values = [m["params"]["progress"] for m in progress]
    assert values == sorted(values) and values[0] == 0
    assert progress[1]["params"]["message"] == "running 1/3 job 1"
    assert messages[reply]["result"]["isError"] is False


@pytest.mark.parametrize("meta", [None, {}, {"progressToken": True}, {"progressToken": [1]}])
def test_no_progress_notifications_without_a_token(home, tmp_path, fake_strata, meta):
    messages = serve_a_batch(home, tmp_path, fake_strata, meta=meta)
    assert [m.get("id") for m in messages] == [11]
    assert not any(m.get("method") == "notifications/progress" for m in messages)


def test_progress_message_is_short_and_total_is_optional():
    from gpu_gate.server import progress_params
    p = progress_params("t", {"state": "running", "progress": {"done": 1, "total": None, "current": "x" * 300}})
    assert p == {"progressToken": "t", "progress": 1, "message": p["message"]}
    assert len(p["message"]) <= 120 and p["message"].startswith("running 1/? x")
    assert progress_params("t", {"state": "pending"}) == {"progressToken": "t", "progress": 0, "message": "pending 0/?"}


def test_lock_is_released_when_the_spawn_fails_and_the_disk_does_too(server, home, spawn, monkeypatch):
    real_write_json = state.write_json

    def broken_spawn(batch_path):
        spawn.paths.append(batch_path)
        raise OSError("no such interpreter")

    def write_json(path, data):
        if spawn.paths:  # once the spawn has been tried, the disk fails as well
            raise OSError("disk full")
        real_write_json(path, data)

    server.spawn_worker = broken_spawn
    monkeypatch.setattr(state, "write_json", write_json)

    res = call(server, "comfy_batch", {"jobs": JOBS})

    assert res["isError"] is True
    assert "could not start the worker" in res["structuredContent"]["error"]
    assert not state.paths(home).lock.exists()


def test_lock_is_released_when_anything_before_the_spawn_raises(server, home, spawn, monkeypatch):
    def boom(rid, batch_id):
        raise RuntimeError("unexpected")

    monkeypatch.setattr(server, "_register_batch", boom)

    res = call(server, "comfy_batch", {"jobs": JOBS})

    assert res["isError"] is True
    assert res["structuredContent"]["error"] == "internal error in comfy_batch: RuntimeError: unexpected"
    assert not state.paths(home).lock.exists()
    assert spawn.paths == []


def test_unhashable_ids_do_not_end_the_loop(server, spawn):
    lines = [
        {"jsonrpc": "2.0", "id": [1], "method": "tools/call",
         "params": {"name": "comfy_batch", "arguments": {"jobs": JOBS}}},
        {"jsonrpc": "2.0", "id": {"a": 1}, "method": "ping"},
        {"jsonrpc": "2.0", "method": "notifications/cancelled", "params": {"requestId": {"a": 1}}},
        {"jsonrpc": "2.0", "method": "notifications/cancelled", "params": {"requestId": [7]}},
        rpc("ping", id=9),
    ]
    inp = io.BytesIO(b"".join(json.dumps(m).encode("utf-8") + b"\n" for m in lines))
    out = io.BytesIO()

    server.serve(inp, out)

    replies = [json.loads(line) for line in out.getvalue().decode("utf-8").splitlines()]
    assert [r.get("error", {}).get("code") for r in replies[:2]] == [-32600, -32600]
    assert all(r["id"] is None for r in replies[:2])
    assert replies[2] == {"jsonrpc": "2.0", "id": 9, "result": {}}
    assert len(replies) == 3  # the two cancels were ignored
    assert spawn.paths == []


def test_arguments_that_are_not_an_object_are_named(server):
    res = call(server, "comfy_batch", ["jobs"])
    assert res["isError"] is True and res["structuredContent"]["error"] == "arguments: expected an object"


def test_worker_argv_uses_the_root_launcher(tmp_path):
    argv = worker_argv(str(tmp_path / "b.json"))
    assert argv[0] == sys.executable
    assert argv[1].endswith("gpu_gate_server.py")
    assert argv[2:] == ["--worker", str(tmp_path / "b.json")]
