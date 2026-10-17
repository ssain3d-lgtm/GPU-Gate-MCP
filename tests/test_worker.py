import dataclasses
import json

import pytest

from fakes import FakeClock, fake_deps
from gpu_gate import comfyui, state, strata, voicestudio, worker
from gpu_gate.config import ComfyCfg, Config, Limits, StrataCfg, VoiceCfg


@pytest.fixture
def home(tmp_path, monkeypatch):
    monkeypatch.setenv(state.ENV_HOME, str(tmp_path / "home"))
    return state.home()


@pytest.fixture
def cfg(tmp_path):
    return Config(
        strata=StrataCfg(),
        comfyui=ComfyCfg(dir=str(tmp_path), bat="Start ComfyUI.bat"),
        voicestudio=VoiceCfg(),
        limits=Limits(),
        other_llm_ports=[8081],
        path=str(tmp_path / "config.json"),
    )


@pytest.fixture
def logs():
    return []


@pytest.fixture
def log(logs):
    return logs.append


@pytest.fixture
def wf(tmp_path):
    """wf("a.json") -> a workflow job whose file exists; wf("x.json", exists=False) -> one that does not."""
    folder = tmp_path / "workflows"
    folder.mkdir()

    def make(name, exists=True, **extra):
        path = folder / name
        if exists:
            path.write_text('{"1": {"class_type": "SaveImage", "inputs": {}}}', encoding="utf-8")
        return {"kind": "workflow", "path": str(path), **extra}

    return make


def batch(jobs, options=None, kind="comfyui"):
    return {
        "batch_id": state.new_batch_id(),
        "kind": kind,
        "jobs": jobs,
        "options": options or {},
        "state": "pending",
        "progress": {"done": 0, "total": len(jobs), "current": None},
        "warnings": [],
        "result": None,
        "started": None,
        "finished": None,
        "pid": None,
    }


def names(deps):
    return [c[0] for c in deps.calls]


def calls_to(deps, name):
    return [c for c in deps.calls if c[0] == name]


def test_happy_path_runs_the_six_steps_in_order(home, cfg, log, wf):
    deps = fake_deps()
    r = worker.run_batch(home, batch(jobs=[wf("a.json"), wf("b.json")]), cfg, deps, log)
    assert [c[0] for c in deps.calls] == ["other_llm_alive", "strata_health", "strata_pause", "exec_ready", "exec_start", "exec_wait_ready", "exec_submit", "exec_submit", "exec_wait", "exec_wait", "exec_free", "exec_stop", "pid_vram_gone", "strata_resume"]
    assert r["ok"] and [j["status"] for j in r["jobs"]] == ["done", "done"] and r["executor"]["started_by_gate"] and r["strata"]["restored"]
    assert [j["name"] for j in r["jobs"]] == ["a.json", "b.json"]
    assert r["jobs"][0]["outputs"] == ["out/p1.png"]
    assert r["executor"] == {"kind": "comfyui", "started_by_gate": True, "cold_start_s": 12.0, "stopped": True}
    assert r["strata"] == {"server": True, "paused_s": 3.0, "restored": True, "load_s": 30.0}
    assert r["error"] is None and r["warnings"] == []
    assert r["log"] == str(state.log_path(home, r["batch_id"]))
    assert set(r) == {"batch_id", "started", "finished", "ok", "strata", "executor", "jobs", "warnings", "log", "error"}
    assert set(r["jobs"][0]) == {"index", "kind", "name", "status", "seconds", "outputs", "error"}
    assert calls_to(deps, "pid_vram_gone")[0][1] == (4242, 60)


def test_strata_is_resumed_even_when_executor_start_fails(home, cfg, log, wf):
    deps = fake_deps(cold_start_s=None)
    r = worker.run_batch(home, batch(jobs=[wf("a.json"), wf("b.json")]), cfg, deps, log)
    assert r["ok"] is False and all(j["status"] == "skipped" for j in r["jobs"]) and ("strata_resume" in names(deps))
    assert "exec_submit" not in names(deps)
    assert "exec_stop" in names(deps)  # the half-started process is stopped
    assert "not ready" in r["error"]


def test_pause_gives_up_after_15_minutes_without_touching_anything(home, cfg, log, wf):
    deps = fake_deps(pause={"status": "busy", "paused_s": 900.0, "tries": 16})
    b = batch(jobs=[wf("a.json")])
    r = worker.run_batch(home, b, cfg, deps, log)
    assert "exec_start" not in names(deps) and "strata_resume" not in names(deps) and r["ok"] is False
    assert names(deps) == ["other_llm_alive", "strata_health", "strata_pause"]
    assert "busy" in r["error"] and r["jobs"][0]["status"] == "skipped"
    assert state.read_state(home)["state"] == "failed"


def test_pause_error_still_resumes_but_runs_nothing(home, cfg, log, wf):
    deps = fake_deps(pause={"status": "error", "error": "unload request got no answer", "paused_s": 330.0, "tries": 1})
    r = worker.run_batch(home, batch(jobs=[wf("a.json")]), cfg, deps, log)
    assert names(deps) == ["other_llm_alive", "strata_health", "strata_pause", "strata_resume"]
    assert r["ok"] is False and "unload request got no answer" in r["error"]
    assert r["jobs"][0]["status"] == "skipped" and r["strata"]["restored"] is True


def test_engine_reloaded_mid_batch_is_warned_not_fought(home, cfg, log, wf):
    # Health: loaded before the pause, and loaded again from the watch's first look on (call 2).
    # ComfyUI then cannot get its memory: the second job fails, the resume finds the engine up.
    deps = fake_deps(
        health=[{"status": "ok", "loaded": True}, {"status": "ok", "loaded": True}],
        wait_advance=10,
        waits=["done", {"status": "error", "error": "CUDA out of memory"}],
        resume={"restored": True, "load_s": 0.0, "error": None, "already_loaded": True},
    )
    r = worker.run_batch(home, batch(jobs=[wf("a.json"), wf("b.json")]), cfg, deps, log)
    assert names(deps).count("strata_pause") == 1 and any("came back" in w for w in r["warnings"]) and r["strata"]["restored"]
    assert names(deps).count("strata_health") == 2  # one warning, then the watch stops looking
    assert len([w for w in r["warnings"] if "came back" in w]) == 1
    assert [j["status"] for j in r["jobs"]] == ["done", "error"]
    assert r["jobs"][1]["error"] == "CUDA out of memory"
    assert r["ok"] is False


def test_other_llm_blocks_unless_forced(home, cfg, log, wf):
    deps = fake_deps(other_llm=[8081])
    r = worker.run_batch(home, batch(jobs=[wf("a.json")]), cfg, deps, log)
    assert r["ok"] is False and "8081" in r["error"] and r["error"].startswith("other_llm_running")
    assert names(deps) == ["other_llm_alive"]
    assert calls_to(deps, "other_llm_alive")[0][1] == ([8081],)

    deps = fake_deps(other_llm=[8081])
    r = worker.run_batch(home, batch([wf("a.json")], {"force": True}), cfg, deps, log)
    assert r["ok"] and "strata_pause" in names(deps)
    assert any("8081" in w for w in r["warnings"])


def test_external_executor_is_reused_and_stopped(home, cfg, log, wf):
    deps = fake_deps(comfy_running=True)
    r = worker.run_batch(home, batch(jobs=[wf("a.json")]), cfg, deps, log)
    assert "exec_start" not in names(deps) and "exec_wait_ready" not in names(deps)
    assert r["executor"]["started_by_gate"] is False and r["executor"]["cold_start_s"] is None
    assert calls_to(deps, "exec_stop")[0][1][0] is None  # stopped by its port, not by a proc
    assert r["executor"]["stopped"] and r["ok"]

    deps = fake_deps(comfy_running=True)
    r = worker.run_batch(home, batch([wf("a.json")], {"keep_executor": True}), cfg, deps, log)
    assert "exec_free" in names(deps) and "exec_stop" not in names(deps) and "pid_vram_gone" not in names(deps)
    assert r["executor"]["stopped"] is False and r["ok"]


def test_keep_executor_does_not_keep_one_the_gate_started(home, cfg, log, wf):
    deps = fake_deps()
    r = worker.run_batch(home, batch([wf("a.json")], {"keep_executor": True}), cfg, deps, log)
    assert "exec_stop" in names(deps) and r["executor"]["stopped"]


def test_cancel_flag_skips_remaining_jobs_and_restores(home, cfg, log, wf):
    b = batch(jobs=[wf("a.json"), wf("b.json"), wf("c.json")])
    deps = fake_deps(on_wait=lambda i: state.request_cancel(home, b["batch_id"]) if i == 0 else None)
    r = worker.run_batch(home, b, cfg, deps, log)
    assert [j["status"] for j in r["jobs"]] == ["done", "cancelled", "cancelled"]
    assert names(deps).count("exec_wait") == 2  # the third prompt is never waited for
    assert calls_to(deps, "exec_delete_pending") == [("exec_delete_pending", (["p2", "p3"],), {})]
    assert "strata_resume" in names(deps) and "exec_stop" in names(deps)
    assert r["ok"] is False and r["strata"]["restored"]


def test_stop_on_error_skips_the_rest(home, cfg, log, wf):
    jobs = [wf("a.json"), wf("b.json"), wf("c.json")]
    deps = fake_deps(waits=[{"status": "error", "error": "node 3 failed"}])
    r = worker.run_batch(home, batch(jobs, {"stop_on_error": True}), cfg, deps, log)
    assert [j["status"] for j in r["jobs"]] == ["error", "skipped", "skipped"]
    assert names(deps).count("exec_wait") == 1
    assert calls_to(deps, "exec_delete_pending") == [("exec_delete_pending", (["p2", "p3"],), {})]
    assert "strata_resume" in names(deps)

    deps = fake_deps(waits=[{"status": "error", "error": "node 3 failed"}])
    r = worker.run_batch(home, batch(jobs), cfg, deps, log)
    assert [j["status"] for j in r["jobs"]] == ["error", "done", "done"]


def test_missing_workflow_file_fails_that_job_only(home, cfg, log, wf):
    jobs = [wf("a.json"), wf("gone.json", exists=False), wf("c.json")]
    deps = fake_deps(load_errors={"c.json": ValueError("not an API-format workflow (save with 'Export (API)')")})
    r = worker.run_batch(home, batch(jobs), cfg, deps, log)
    assert [j["status"] for j in r["jobs"]] == ["done", "error", "error"]
    assert "not found" in r["jobs"][1]["error"] and "gone.json" in r["jobs"][1]["error"]
    assert "Export (API)" in r["jobs"][2]["error"]
    assert names(deps).count("exec_submit") == 1 and names(deps).count("exec_wait") == 1
    assert r["ok"] is False and r["error"] is None and "strata_resume" in names(deps)


def test_command_jobs_run_sequentially_with_cwd(home, cfg, log, tmp_path):
    first, second = tmp_path / "plan", tmp_path / "tools"
    first.mkdir()
    second.mkdir()
    jobs = [
        {"kind": "command", "command": ["python", "gen.py", "--spec", "x"], "cwd": str(first)},
        {"kind": "command", "command": ["tool"], "cwd": str(second), "timeout_s": 60, "name": "tool run"},
    ]
    deps = fake_deps(commands=[(0, 1.5), (3, 0.5)])
    r = worker.run_batch(home, batch(jobs), cfg, deps, log)
    runs = calls_to(deps, "run_command")
    assert [c[1][:3] for c in runs] == [
        (["python", "gen.py", "--spec", "x"], str(first), 1800),
        (["tool"], str(second), 60),
    ]
    assert runs[0][1][4] == str(state.log_path(home, r["batch_id"]))  # output goes to the batch log
    assert [j["status"] for j in r["jobs"]] == ["done", "error"]
    assert r["jobs"][1]["error"] == "exited 3" and r["jobs"][0]["seconds"] == 1.5
    assert [j["name"] for j in r["jobs"]] == ["python", "tool run"]
    assert "exec_submit" not in names(deps)
    assert names(deps).index("exec_wait_ready") < names(deps).index("run_command")


def test_unknown_kind_fails_that_job(home, cfg, log, wf):
    deps = fake_deps()
    r = worker.run_batch(home, batch([{"kind": "video", "path": "x"}, wf("a.json")]), cfg, deps, log)
    assert [j["status"] for j in r["jobs"]] == ["error", "done"]
    assert "unknown kind" in r["jobs"][0]["error"]


def test_comfy_variant_option_reaches_exec_start(home, cfg, log, wf):
    deps = fake_deps()
    options = {"comfy_variant": "Start ComfyUI SageAttention.bat"}
    worker.run_batch(home, batch([wf("a.json")], options), cfg, deps, log)
    assert calls_to(deps, "exec_start")[0][2] == {"bat_override": "Start ComfyUI SageAttention.bat"}

    deps = fake_deps(start_error=ValueError("comfy_variant not found: Nope.bat"))
    r = worker.run_batch(home, batch([wf("a.json"), wf("b.json")], {"comfy_variant": "Nope.bat"}), cfg, deps, log)
    assert all(j["status"] == "skipped" for j in r["jobs"]) and "comfy_variant" in r["error"]
    assert "strata_resume" in names(deps) and r["strata"]["restored"]
    assert "exec_stop" not in names(deps)  # nothing was started, nothing to stop


def test_exception_mid_batch_still_cleans_up_and_resumes(home, cfg, log, logs, wf):
    deps = fake_deps(submit_error=RuntimeError("boom\nsecond line"))
    r = worker.run_batch(home, batch([wf("a.json")]), cfg, deps, log)
    assert r["error"] == "RuntimeError: boom second line"
    assert r["ok"] is False and r["jobs"][0]["status"] == "skipped"
    assert names(deps)[-4:] == ["exec_free", "exec_stop", "pid_vram_gone", "strata_resume"]
    assert any("Traceback" in line for line in logs)


def test_submit_error_fails_that_job_only(home, cfg, log, wf):
    deps = fake_deps(submit_error=comfyui.SubmitError("node 3: Required input is missing: images"))
    r = worker.run_batch(home, batch([wf("a.json")]), cfg, deps, log)
    assert r["jobs"][0]["status"] == "error" and "node 3" in r["jobs"][0]["error"]
    assert r["error"] is None and "exec_wait" not in names(deps)


def test_batch_timeout_marks_unwaited_jobs(home, cfg, log, wf):
    deps = fake_deps(wait_advance=100)
    r = worker.run_batch(home, batch([wf("a.json"), wf("b.json")], {"batch_timeout_s": 50}), cfg, deps, log)
    assert [j["status"] for j in r["jobs"]] == ["done", "timeout"]
    assert calls_to(deps, "exec_wait")[0][1][1] == 50  # a wait never outlives the batch


def test_voice_batch_runs_the_script_once(home, cfg, log):
    jobs = [{"text": "hello", "out": "voice/a.wav"}, {"text": "bye", "out": "voice/b.wav"}]
    deps = fake_deps()
    r = worker.run_batch(home, batch(jobs, kind="voicestudio"), cfg, deps, log)
    run = calls_to(deps, "voice_run_batch")
    assert len(run) == 1 and run[0][1][0] == jobs
    assert run[0][1][1] == str(state.paths(home).batches_dir / r["batch_id"])
    assert [(j["kind"], j["name"], j["status"]) for j in r["jobs"]] == [("voice", "a.wav", "done"), ("voice", "b.wav", "done")]
    assert r["executor"]["kind"] == "voicestudio" and r["ok"]
    assert "exec_submit" not in names(deps)


def test_empty_jobs_is_a_value_error(home, cfg, log):
    with pytest.raises(ValueError):
        worker.run_batch(home, batch([]), cfg, fake_deps(), log)


def test_state_json_and_batch_file_follow_the_transitions(home, cfg, log, wf, monkeypatch):
    seen = []
    real = state.set_state

    def spy(home, st, batch_id=None, last_result=None):
        seen.append(st)
        real(home, st, batch_id, last_result)

    monkeypatch.setattr(state, "set_state", spy)
    b = batch([wf("a.json"), wf("b.json")])
    r = worker.run_batch(home, b, cfg, fake_deps(), log)
    assert seen == ["pausing", "starting", "running", "cleanup", "restoring", "done"]
    assert state.read_state(home)["state"] == "done"
    assert state.read_state(home)["last_result"] == r
    doc = state.read_json(state.batch_path(home, b["batch_id"]))
    assert doc["result"] == r
    assert doc["state"] == "done" and doc["finished"] == r["finished"] and doc["started"] == r["started"]
    assert doc["progress"] == {"done": 2, "total": 2, "current": None}
    assert state.read_json(state.paths(home).lock)["batch_id"] == b["batch_id"]  # run_batch does not release it


def test_result_never_contains_env_values(home, cfg, log, logs, wf, tmp_path, monkeypatch):
    monkeypatch.setenv("GPU_GATE_TEST_TOKEN", "secret-value")
    cfg = dataclasses.replace(cfg, strata=StrataCfg(api_key="secret-value"))
    jobs = [wf("a.json"), {"kind": "command", "command": ["python", "gen.py"], "cwd": str(tmp_path)}]
    b = batch(jobs)
    r = worker.run_batch(home, b, cfg, fake_deps(), log)
    assert "secret-value" not in json.dumps(r)
    assert "secret-value" not in state.batch_path(home, b["batch_id"]).read_text(encoding="utf-8")
    assert "secret-value" not in state.paths(home).state.read_text(encoding="utf-8")
    assert "secret-value" not in "\n".join(logs)


def _write_config(home, tmp_path):
    (tmp_path / "comfy").mkdir()
    config = {"comfyui": {"dir": str(tmp_path / "comfy"), "bat": "Start ComfyUI.bat"}, "other_llm_ports": []}
    state.paths(home).config.write_text(json.dumps(config), encoding="utf-8")


def test_main_writes_the_result_and_releases_the_lock(home, wf, tmp_path, monkeypatch):
    _write_config(home, tmp_path)
    deps = fake_deps()
    kinds = []
    monkeypatch.setattr(worker, "default_deps", lambda cfg, kind: kinds.append(kind) or deps)
    b = batch([wf("a.json")])
    path = state.batch_path(home, b["batch_id"])
    state.write_json(path, b)
    state.acquire_lock(home, b["batch_id"])

    assert worker.main(str(path)) == 0
    doc = state.read_json(path)
    assert doc["result"]["ok"] and doc["finished"] and doc["state"] == "done"
    assert state.read_state(home)["state"] == "done"
    assert state.read_json(state.paths(home).lock) is None
    assert kinds == ["comfyui"]
    text = state.log_path(home, b["batch_id"]).read_text(encoding="utf-8")
    assert "strata" in text and text.splitlines()[0][:4].isdigit()  # "<iso> <msg>" lines


def test_main_reports_a_missing_config(home, wf):
    b = batch([wf("a.json")])
    path = state.batch_path(home, b["batch_id"])
    state.write_json(path, b)
    state.acquire_lock(home, b["batch_id"])

    assert worker.main(str(path)) == 1
    doc = state.read_json(path)
    assert "config not found" in doc["result"]["error"] and doc["result"]["ok"] is False
    assert doc["state"] == "failed" and doc["finished"]
    assert [j["status"] for j in doc["result"]["jobs"]] == ["skipped"]
    assert state.read_state(home)["state"] == "failed"
    assert state.read_json(state.paths(home).lock) is None


def test_main_leaves_another_batchs_lock_alone(home, wf, tmp_path, monkeypatch):
    _write_config(home, tmp_path)
    monkeypatch.setattr(worker, "default_deps", lambda cfg, kind: fake_deps())
    b = batch([wf("a.json")])
    path = state.batch_path(home, b["batch_id"])
    state.write_json(path, b)
    # Another batch holds the lock (it took over what it saw as a stale lock while we ran).
    state.write_json(state.paths(home).lock, {"batch_id": "other", "pid": 1, "started": "x"})
    monkeypatch.setattr(state, "write_lock", lambda *args: None)
    assert worker.main(str(path)) == 0
    assert state.read_json(state.paths(home).lock)["batch_id"] == "other"


def test_kept_executor_gets_unfinished_prompts_off_the_queue(home, cfg, log, wf):
    # The batch runs out of time: the first prompt timed out (still in ComfyUI), the other two were
    # never waited for. The kept ComfyUI must not go on with them while Strata loads again.
    deps = fake_deps(comfy_running=True, wait_advance=100, waits=["timeout"])
    jobs = [wf("a.json"), wf("b.json"), wf("c.json")]
    r = worker.run_batch(home, batch(jobs, {"keep_executor": True, "batch_timeout_s": 50}), cfg, deps, log)
    assert [j["status"] for j in r["jobs"]] == ["timeout", "timeout", "timeout"]
    assert calls_to(deps, "exec_delete_pending") == [("exec_delete_pending", (["p1", "p2", "p3"],), {})]
    assert names(deps).index("exec_delete_pending") < names(deps).index("exec_free")
    assert "exec_stop" not in names(deps) and "strata_resume" in names(deps)
    assert any("taken off the queue" in w for w in r["warnings"])


def test_mixed_jobs_run_in_segments(home, cfg, log, wf, tmp_path):
    steps = ("exec_submit", "exec_wait", "run_command")
    command = {"kind": "command", "command": ["python", "post.py"], "cwd": str(tmp_path)}
    deps = fake_deps()
    r = worker.run_batch(home, batch([wf("a.json"), command, wf("b.json")]), cfg, deps, log)
    assert [n for n in names(deps) if n in steps] == ["exec_submit", "exec_wait", "run_command", "exec_submit", "exec_wait"]
    assert r["ok"]

    deps = fake_deps()
    worker.run_batch(home, batch([wf("a.json"), wf("b.json")]), cfg, deps, log)
    assert [n for n in names(deps) if n in steps] == ["exec_submit", "exec_submit", "exec_wait", "exec_wait"]


def test_vram_reading_failure_keeps_the_results(home, wf, tmp_path, monkeypatch):
    _write_config(home, tmp_path)

    def broken_gauge():
        raise RuntimeError("nvidia-smi exploded")

    def deps_with_a_broken_gauge(cfg, kind):
        deps = fake_deps()
        deps.vram_used = broken_gauge
        return deps

    monkeypatch.setattr(worker, "default_deps", deps_with_a_broken_gauge)
    b = batch([wf("a.json")])
    path = state.batch_path(home, b["batch_id"])
    state.write_json(path, b)
    assert worker.main(str(path)) == 0
    result = state.read_json(path)["result"]
    assert [j["status"] for j in result["jobs"]] == ["done"] and result["strata"]["restored"] is True
    assert any("VRAM reading failed" in w for w in result["warnings"])

    # An exception after run_batch wrote its result must not replace it with a bare failure.
    real_run_batch = worker.run_batch

    def run_then_fail(*args):
        real_run_batch(*args)
        raise RuntimeError("late failure")

    monkeypatch.setattr(worker, "run_batch", run_then_fail)
    b = batch([wf("b.json")])
    path = state.batch_path(home, b["batch_id"])
    state.write_json(path, b)
    assert worker.main(str(path)) == 1
    result = state.read_json(path)["result"]
    assert [j["status"] for j in result["jobs"]] == ["done"] and result["strata"]["restored"] is True


def test_unreadable_batch_file_still_releases_the_lock(home):
    batch_id = "20261017-120000-abcd"
    path = state.batch_path(home, batch_id)
    path.write_text("{not json", encoding="utf-8")
    state.write_lock(home, batch_id, 1)
    assert worker.main(str(path)) == 1
    assert state.read_json(state.paths(home).lock) is None


def test_keyboard_interrupt_still_stops_and_resumes(home, wf, tmp_path, monkeypatch):
    _write_config(home, tmp_path)
    deps = fake_deps(waits=[KeyboardInterrupt()])
    monkeypatch.setattr(worker, "default_deps", lambda cfg, kind: deps)
    b = batch([wf("a.json")])
    path = state.batch_path(home, b["batch_id"])
    state.write_json(path, b)
    with pytest.raises(KeyboardInterrupt):
        worker.main(str(path))
    assert "exec_stop" in names(deps) and "strata_resume" in names(deps)
    assert names(deps).index("exec_stop") < names(deps).index("strata_resume")
    assert state.read_json(state.paths(home).lock) is None


def test_default_deps_binds_the_adapters_by_kind(cfg):
    d = worker.default_deps(cfg, "comfyui")
    assert d.strata_health.func is strata.health and d.strata_health.args == (cfg.strata,)
    assert d.strata_pause.func is strata.pause and d.strata_resume.func is strata.resume
    assert d.exec_ready.func is comfyui.ready and d.exec_ready.args == (cfg.comfyui,)
    assert d.exec_start.func is comfyui.start and d.exec_wait_ready.func is comfyui.wait_ready
    assert d.exec_submit.func is comfyui.submit and d.exec_wait.func is comfyui.wait
    assert d.exec_free.func is comfyui.free and d.exec_stop.func is comfyui.stop
    assert d.exec_delete_pending.func is comfyui.delete_pending
    assert d.exec_load_workflow is comfyui.load_workflow

    v = worker.default_deps(cfg, "voicestudio")
    assert v.exec_ready.func is voicestudio.ready and v.exec_ready.args == (cfg.voicestudio,)
    assert v.exec_start.func is voicestudio.start and v.exec_wait_ready.func is voicestudio.wait_ready
    assert v.exec_stop.func is voicestudio.stop
    assert v.voice_run_batch.func is voicestudio.run_batch and v.voice_run_batch.args == (cfg.voicestudio,)
    assert v.exec_free() is True  # VoiceStudio has no /free; its batch script unloads the model

    with pytest.raises(ValueError):
        worker.default_deps(cfg, "blender")
