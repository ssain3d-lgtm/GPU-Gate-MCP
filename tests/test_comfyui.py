import json
import os
import subprocess
import sys
from types import SimpleNamespace

import pytest

from fakes import FakeServer
from gpu_gate import comfyui, procs
from gpu_gate.config import ComfyCfg

# The shape of a real "Start ComfyUI.bat" (with made-up values): an env line holding a key, quoted
# `set`s, comment-ish lines, and a relative python line.
BAT = (
    "set LM_API=secret-value\r\n"
    "@Echo off&&cd /D %~dp0\r\n"
    'set "PATH=%~dp0Add-Ons\\ffmpeg\\bin;%PATH%"\r\n'
    'set "PIP_CONSTRAINT=%~dp0python_constraints.txt"\r\n'
    "Title X\r\n"
    ".\\python_embeded\\python.exe -I -W ignore::FutureWarning ComfyUI\\main.py --windows-standalone-build\r\n"
)


@pytest.fixture
def bat(tmp_path):
    path = tmp_path / "Start ComfyUI.bat"
    path.write_bytes(BAT.encode("utf-8"))
    return path


class Lines(list):
    """A log callable that collects the lines it is given."""

    def __call__(self, msg):
        self.append(msg)


@pytest.fixture
def lines():
    return Lines()


class Mute(FakeServer):
    """Accepts the connection and hangs up without answering: what a ComfyUI that is not there looks like."""

    def handle(self, method, path, body):
        return None


@pytest.fixture
def silent_cfg():
    """A ComfyCfg whose server never answers."""
    fake = Mute()
    yield ComfyCfg(dir=".", bat="x.bat", url=fake.start())
    fake.stop()


# --- bat parsing -----------------------------------------------------------------------------


def test_parse_bat_expands_dp0_and_resolves_relative_paths(tmp_path, bat):
    argv, env = comfyui.parse_bat(str(bat))
    assert argv[0] == str(tmp_path / "python_embeded" / "python.exe")
    assert argv[-2:] == ["--windows-standalone-build", "--disable-auto-launch"]
    assert argv[1:4] == ["-I", "-W", "ignore::FutureWarning"]
    assert argv[4] == str(tmp_path / "ComfyUI" / "main.py")
    assert env["PATH"].startswith(str(tmp_path) + os.sep + "Add-Ons")
    assert env["PATH"].endswith(os.environ["PATH"])  # %PATH% came from the real environment
    assert env["LM_API"] == "secret-value"
    assert env["PIP_CONSTRAINT"].endswith("python_constraints.txt")
    assert "%" not in env["PATH"] + env["PIP_CONSTRAINT"]


def test_parse_bat_keeps_an_existing_disable_auto_launch(tmp_path):
    bat = tmp_path / "x.bat"
    bat.write_text("python.exe ComfyUI\\main.py --disable-auto-launch\r\n", encoding="utf-8")
    argv, env = comfyui.parse_bat(str(bat))
    assert argv.count("--disable-auto-launch") == 1 and argv[-1] == "--disable-auto-launch"
    assert argv[0] == "python.exe"  # a bare command name is looked up on PATH, not in the bat folder
    assert env == {}


def test_parse_bat_reads_quoted_set_and_python_tokens(tmp_path):
    bat = tmp_path / "x.bat"
    python = tmp_path / "Program Files" / "python.exe"
    bat.write_text(
        'SET "Name=with space"\r\nREM "python" ComfyUI\\main.py\r\n'
        f'"{python}" "%~dp0ComfyUI\\main.py" --tmp "%Name%"\r\n',
        encoding="utf-8",
    )
    argv, env = comfyui.parse_bat(str(bat))
    assert env == {"Name": "with space"}
    assert argv[0] == str(python)
    assert argv[1] == str(tmp_path / "ComfyUI" / "main.py")
    assert argv[2:] == ["--tmp", "with space", "--disable-auto-launch"]


def test_parse_bat_without_python_line_raises(tmp_path):
    bat = tmp_path / "x.bat"
    bat.write_text("@echo off\r\nset A=1\r\npause\r\n", encoding="utf-8")
    with pytest.raises(ValueError, match="no python line with main.py in .*x.bat"):
        comfyui.parse_bat(str(bat))


def test_env_keys_never_contain_values():
    assert comfyui.env_keys({"LM_API": "secret-value"}) == ["LM_API"]
    assert comfyui.env_keys({"PIP_CONSTRAINT": "x", "PATH": "y"}) == ["PATH", "PIP_CONSTRAINT"]


def test_command_prefers_the_configured_command_over_the_bat(tmp_path, bat):
    cfg = ComfyCfg(dir=str(tmp_path), bat=bat.name, command=["py", "main.py"])
    assert comfyui.command(cfg) == (["py", "main.py"], {}, str(tmp_path))


def test_command_parses_the_bat_and_runs_in_its_folder(tmp_path, bat):
    cfg = ComfyCfg(dir=str(tmp_path), bat=bat.name)
    argv, env, cwd = comfyui.command(cfg)
    assert argv[0] == str(tmp_path / "python_embeded" / "python.exe")
    assert env["LM_API"] == "secret-value"
    assert cwd == str(tmp_path)


def test_command_bat_override_names_another_bat_in_the_folder(tmp_path, bat):
    other = tmp_path / "Start Other.bat"
    other.write_text("py ComfyUI\\main.py --lowvram\r\n", encoding="utf-8")
    cfg = ComfyCfg(dir=str(tmp_path), bat=bat.name)
    argv, env, cwd = comfyui.command(cfg, "Start Other.bat")
    assert argv[0] == "py" and argv[-2:] == ["--lowvram", "--disable-auto-launch"] and env == {}


@pytest.mark.parametrize("name", ["missing.bat", "../escape.bat", "sub/other.bat", "sub\\other.bat", ".."])
def test_command_bat_override_must_be_an_existing_bare_file_name(tmp_path, name):
    folder = tmp_path / "ComfyUI"
    (folder / "sub").mkdir(parents=True)
    for other in (tmp_path / "escape.bat", folder / "sub" / "other.bat"):
        other.write_text("py ComfyUI\\main.py\r\n", encoding="utf-8")
    cfg = ComfyCfg(dir=str(folder), bat="Start ComfyUI.bat")
    with pytest.raises(ValueError) as e:
        comfyui.command(cfg, name)
    assert str(e.value) == f"comfy_variant not found: {name}"
    assert comfyui.variant_error(cfg, name) == f"comfy_variant not found: {name}"


def test_variant_error_accepts_what_command_accepts(tmp_path):
    (tmp_path / "Start Other.bat").write_text("py ComfyUI\\main.py\r\n", encoding="utf-8")
    cfg = ComfyCfg(dir=str(tmp_path), bat="Start ComfyUI.bat")
    assert comfyui.variant_error(cfg, "Start Other.bat") is None
    assert comfyui.variant_error(cfg, 7) == "comfy_variant not found: 7"
    # A configured command wins over any bat, so command() never looks at the variant.
    with_command = ComfyCfg(dir=str(tmp_path), command=["python", "main.py"])
    assert comfyui.variant_error(with_command, "missing.bat") is None
    assert comfyui.command(with_command, "missing.bat")[0] == ["python", "main.py"]


# --- workflow files --------------------------------------------------------------------------


def test_load_workflow_rejects_ui_format(tmp_path):
    path = tmp_path / "ui.json"
    path.write_text(json.dumps({"nodes": [], "links": []}), encoding="utf-8")
    with pytest.raises(ValueError) as e:
        comfyui.load_workflow(str(path))
    assert "Export (API)" in str(e.value) and str(path) in str(e.value)


def test_load_workflow_returns_an_api_format_dict(tmp_path):
    workflow = {"3": {"class_type": "KSampler", "inputs": {"seed": 1}}}
    path = tmp_path / "api.json"
    path.write_text(json.dumps(workflow), encoding="utf-8-sig")  # a BOM from a Windows editor
    assert comfyui.load_workflow(str(path)) == workflow


@pytest.mark.parametrize("text", ["[1, 2]", "not json"])
def test_load_workflow_rejects_what_is_not_a_workflow(tmp_path, text):
    path = tmp_path / "bad.json"
    path.write_text(text, encoding="utf-8")
    with pytest.raises(ValueError) as e:
        comfyui.load_workflow(str(path))
    assert str(path) in str(e.value)


# --- readiness and start ---------------------------------------------------------------------


def test_ready_is_true_only_when_system_stats_answers(fake_comfy, silent_cfg):
    fake, cfg = fake_comfy
    assert comfyui.ready(cfg) is True
    assert ("GET", "/system_stats") in fake.calls
    assert comfyui.ready(silent_cfg) is False


def test_wait_ready_returns_the_seconds_it_took(fake_comfy, clock):
    fake, cfg = fake_comfy
    proc = SimpleNamespace(poll=lambda: None)
    assert comfyui.wait_ready(cfg, proc, 60, clock.sleep, clock.now) == 0.0
    assert clock.sleeps == []


def test_wait_ready_polls_until_the_server_answers(monkeypatch, clock):
    answers = iter([False, False, True])
    monkeypatch.setattr(comfyui, "ready", lambda cfg: next(answers))
    proc = SimpleNamespace(poll=lambda: None)
    seconds = comfyui.wait_ready(ComfyCfg(dir=".", bat="x.bat"), proc, 60, clock.sleep, clock.now)
    assert seconds == 2.0 and clock.sleeps == [1.0, 1.0]


def test_wait_ready_gives_up_when_the_process_dies_first(monkeypatch, clock):
    monkeypatch.setattr(comfyui, "ready", lambda cfg: False)
    proc = SimpleNamespace(poll=lambda: 1)
    assert comfyui.wait_ready(ComfyCfg(dir=".", bat="x.bat"), proc, 60, clock.sleep, clock.now) is None
    assert clock.sleeps == []


def test_wait_ready_gives_up_at_the_timeout(monkeypatch, clock):
    monkeypatch.setattr(comfyui, "ready", lambda cfg: False)
    proc = SimpleNamespace(poll=lambda: None)
    assert comfyui.wait_ready(ComfyCfg(dir=".", bat="x.bat"), proc, 5, clock.sleep, clock.now) is None
    assert clock.sleeps == [1.0] * 5


def test_start_runs_the_bat_command_with_its_env_and_never_logs_env_values(tmp_path, lines):
    (tmp_path / "ComfyUI").mkdir()
    (tmp_path / "ComfyUI" / "main.py").write_text(
        "import os, sys\n"
        "print('LM_API=' + os.environ.get('LM_API', 'missing'))\n"
        "print('ARGV=' + ' '.join(sys.argv[1:]))\n"
        "print('CWD=' + os.getcwd())\n",
        encoding="utf-8",
    )
    bat = tmp_path / "Start ComfyUI.bat"
    bat.write_text(f'set LM_API=secret-value\r\n"{sys.executable}" ComfyUI\\main.py\r\n', encoding="utf-8")
    cfg = ComfyCfg(dir=str(tmp_path), bat=bat.name)
    log_path = tmp_path / "comfy.log"

    proc = comfyui.start(cfg, str(log_path), lines)
    assert proc.wait(60) == 0

    output = log_path.read_text(encoding="utf-8")
    assert "LM_API=secret-value" in output  # the child got the bat's env on top of the real one
    assert "ARGV=--disable-auto-launch" in output
    cwd = next(line for line in output.splitlines() if line.startswith("CWD="))[4:]
    assert os.path.samefile(cwd, tmp_path)
    assert len(lines) == 1 and lines[0].startswith("comfyui: starting ")
    assert "main.py" in lines[0] and lines[0].endswith("(env keys: LM_API)")
    assert "secret-value" not in lines[0]


def test_start_hides_a_bat_value_that_reached_the_argv_but_still_passes_it_on(tmp_path, lines):
    (tmp_path / "ComfyUI").mkdir()
    (tmp_path / "ComfyUI" / "main.py").write_text(
        "import sys\nprint('ARGV=' + ' '.join(sys.argv[1:]))\n", encoding="utf-8"
    )
    bat = tmp_path / "Start ComfyUI.bat"
    bat.write_text(
        f'set LM_API=secret-value\r\nset N=1\r\n"{sys.executable}" ComfyUI\\main.py --api-key %LM_API% --n %N%\r\n',
        encoding="utf-8",
    )
    cfg = ComfyCfg(dir=str(tmp_path), bat=bat.name)
    log_path = tmp_path / "comfy.log"

    argv, _, _ = comfyui.command(cfg)
    # the process needs the real values; --disable-auto-launch is appended last
    assert argv[-5:] == ["--api-key", "secret-value", "--n", "1", "--disable-auto-launch"]

    proc = comfyui.start(cfg, str(log_path), lines)
    assert proc.wait(60) == 0

    assert "ARGV=--api-key secret-value --n 1 --disable-auto-launch" in log_path.read_text(encoding="utf-8")
    assert len(lines) == 1
    assert "secret-value" not in lines[0]
    assert "--api-key <LM_API> --n 1 " in lines[0]  # the 1-character value is not worth hiding
    assert lines[0].endswith("(env keys: LM_API, N)")


def test_redact_argv_replaces_long_bat_values_inside_any_argument():
    env = {"LM_API": "secret-value", "TOKEN": "secret", "N": "1"}
    argv = ["py", "--key=secret-value", "--other", "a-secret-b", "--n", "1"]
    assert comfyui._redact_argv(argv, env) == ["py", "--key=<LM_API>", "--other", "a-<TOKEN>-b", "--n", "1"]
    assert argv[1] == "--key=secret-value"  # the real argv is not touched
    assert comfyui._redact_argv(argv, {}) == argv


def test_start_passes_bat_override_through_to_command(tmp_path, monkeypatch, lines):
    (tmp_path / "Start Other.bat").write_text("py ComfyUI\\main.py\r\n", encoding="utf-8")
    spawned = []
    monkeypatch.setattr(procs, "spawn_detached", lambda *a, **k: spawned.append((a, k)) or "proc")
    cfg = ComfyCfg(dir=str(tmp_path), bat="missing.bat")
    assert comfyui.start(cfg, "log.txt", lines, bat_override="Start Other.bat") == "proc"
    (argv, cwd, log_path), kwargs = spawned[0]
    assert argv[0] == "py" and cwd == str(tmp_path) and log_path == "log.txt"
    assert lines[0].startswith("comfyui: starting py ") and lines[0].endswith("(env keys: none)")
    with pytest.raises(ValueError, match="comfy_variant not found: nope.bat"):
        comfyui.start(cfg, "log.txt", lines, bat_override="nope.bat")


# --- submit ----------------------------------------------------------------------------------

WORKFLOW = {"3": {"class_type": "LoadImage", "inputs": {}}, "9": {"class_type": "SaveImage", "inputs": {}}}


def test_submit_returns_prompt_id(fake_comfy):
    fake, cfg = fake_comfy
    assert comfyui.submit(cfg, WORKFLOW, "client-1") == "p1"
    assert fake.prompts == [{"prompt": WORKFLOW, "client_id": "client-1"}]


def test_submit_reports_node_errors(fake_comfy_reject):
    fake, cfg = fake_comfy_reject
    with pytest.raises(comfyui.SubmitError) as e:
        comfyui.submit(cfg, WORKFLOW, "client-1")
    assert "node 3" in str(e.value) and "images" in str(e.value)
    assert str(e.value) == "node 3: Required input is missing: images"


def test_submit_summary_lists_each_node_and_caps_long_details(fake_comfy):
    fake, cfg = fake_comfy
    body = {
        "error": {"type": "x", "message": "Prompt outputs failed validation", "details": ""},
        "node_errors": {
            "3": {"errors": [{"message": "Required input is missing", "details": "images"}]},
            "7": {"errors": [{"message": "Value not in list", "details": "ckpt_name: " + "x" * 5000}]},
        },
    }
    summary = comfyui._summary(body)
    first, second = summary.split("; node ", 1)
    assert first == "node 3: Required input is missing: images"
    assert second.startswith("7: Value not in list: ckpt_name: xxx") and len(second) < 400


def test_submit_without_node_errors_uses_the_error_message():
    body = {"error": {"type": "prompt_no_outputs", "message": "Prompt has no outputs", "details": ""}, "node_errors": {}}
    assert comfyui._summary(body) == "Prompt has no outputs"
    assert comfyui._summary(None) == ""


def test_submit_fails_clearly_when_nothing_answers(silent_cfg):
    with pytest.raises(comfyui.SubmitError, match="no answer from ComfyUI"):
        comfyui.submit(silent_cfg, WORKFLOW, "client-1")


# --- wait ------------------------------------------------------------------------------------


def test_wait_collects_output_paths(fake_comfy, clock, tmp_path):
    fake, cfg = fake_comfy
    out_dir = tmp_path / "ComfyUI" / "output"
    fake.complete("p1", [("a_00001_.png", "sub", "output"), ("tmp.png", "", "temp"), ("b.png", "", "output")])
    r = comfyui.wait(cfg, "p1", 60, lambda: False, clock.sleep, clock.now)
    assert r["status"] == "done" and r["error"] is None and r["seconds"] == 0.0
    assert r["outputs"] == [str(out_dir / "sub" / "a_00001_.png"), str(out_dir / "b.png")]
    assert ("GET", "/history/p1") in fake.calls


def test_wait_uses_the_configured_output_dir(fake_comfy, clock, tmp_path):
    fake, cfg = fake_comfy
    cfg = ComfyCfg(dir=cfg.dir, bat=cfg.bat, url=cfg.url, output_dir=str(tmp_path / "elsewhere"))
    fake.complete("p1", [("a.png", "", "output")])
    r = comfyui.wait(cfg, "p1", 60, lambda: False, clock.sleep, clock.now)
    assert r["outputs"] == [str(tmp_path / "elsewhere" / "a.png")]


def test_wait_collects_every_kind_of_output(fake_comfy, clock, tmp_path):
    fake, cfg = fake_comfy
    fake.complete("p1")
    fake.history["p1"]["outputs"] = {
        "5": {"gifs": [{"filename": "v.gif", "subfolder": "", "type": "output"}], "text": ["ignored"]},
        "6": {"videos": [{"filename": "v.mp4", "subfolder": "vid", "type": "output"}]},
        "7": {"audio": [{"filename": "s.flac", "subfolder": "", "type": "output"}]},
    }
    r = comfyui.wait(cfg, "p1", 60, lambda: False, clock.sleep, clock.now)
    out_dir = tmp_path / "ComfyUI" / "output"
    assert r["outputs"] == [str(out_dir / "v.gif"), str(out_dir / "vid" / "v.mp4"), str(out_dir / "s.flac")]


def test_wait_polls_every_1_5_seconds_until_the_prompt_finishes(fake_comfy, clock):
    fake, cfg = fake_comfy

    def sleep(seconds):
        clock.sleep(seconds)
        if len(clock.sleeps) == 3:
            fake.complete("p1", [("a.png", "", "output")])

    r = comfyui.wait(cfg, "p1", 60, lambda: False, sleep, clock.now)
    assert r["status"] == "done" and r["seconds"] == 4.5
    assert clock.sleeps == [1.5, 1.5, 1.5]
    assert len(r["outputs"]) == 1


def test_wait_reports_execution_error(fake_comfy, clock):
    fake, cfg = fake_comfy
    fake.complete("p1", error="CUDA out of memory")
    r = comfyui.wait(cfg, "p1", 60, lambda: False, clock.sleep, clock.now)
    assert r["status"] == "error" and r["error"] == "CUDA out of memory" and r["outputs"] == []


def test_wait_error_without_a_message_says_execution_failed(fake_comfy, clock):
    fake, cfg = fake_comfy
    fake.complete("p1", error="x")
    fake.history["p1"]["status"]["messages"] = [["execution_start", {}]]
    r = comfyui.wait(cfg, "p1", 60, lambda: False, clock.sleep, clock.now)
    assert r["status"] == "error" and r["error"] == "execution failed"


def test_wait_times_out(fake_comfy, clock):
    fake, cfg = fake_comfy
    r = comfyui.wait(cfg, "p1", 10, lambda: False, clock.sleep, clock.now)
    assert r["status"] == "timeout" and r["outputs"] == [] and r["error"] is None
    assert clock.sleeps == [1.5] * 7 and r["seconds"] == 10.5
    assert ("POST", "/interrupt") not in fake.calls


def test_wait_cancels_and_interrupts(fake_comfy, clock):
    fake, cfg = fake_comfy
    r = comfyui.wait(cfg, "p1", 60, lambda: True, clock.sleep, clock.now)
    assert r["status"] == "cancelled" and r["outputs"] == [] and r["error"] is None
    assert ("POST", "/interrupt") in fake.calls


def test_wait_does_not_interrupt_a_prompt_that_already_finished(fake_comfy, clock):
    fake, cfg = fake_comfy
    fake.complete("p1", [("a.png", "", "output")])
    r = comfyui.wait(cfg, "p1", 60, lambda: True, clock.sleep, clock.now)
    assert r["status"] == "done"
    assert ("POST", "/interrupt") not in fake.calls


def test_wait_returns_an_error_at_once_when_the_process_has_ended(fake_comfy, clock):
    fake, cfg = fake_comfy
    r = comfyui.wait(cfg, "p1", 60, lambda: False, clock.sleep, clock.now, alive=lambda: False)
    assert r["status"] == "error" and r["error"] == "comfyui process ended" and r["outputs"] == []
    assert fake.calls == [("GET", "/history/p1")]  # one poll, then it gave up
    assert clock.sleeps == []


def test_wait_keeps_polling_while_the_process_is_alive(fake_comfy, clock):
    fake, cfg = fake_comfy
    states = iter([True, True, False])
    r = comfyui.wait(cfg, "p1", 60, lambda: False, clock.sleep, clock.now, alive=lambda: next(states))
    assert r["status"] == "error" and r["error"] == "comfyui process ended"
    assert clock.sleeps == [1.5, 1.5] and r["seconds"] == 3.0
    assert fake.calls.count(("GET", "/history/p1")) == 3


def test_wait_with_a_dead_process_still_lists_the_outputs_already_in_history(fake_comfy, clock, tmp_path):
    fake, cfg = fake_comfy
    fake.complete("p1", [("a.png", "sub", "output")])
    fake.history["p1"]["status"] = {"status_str": "success", "completed": False, "messages": []}
    r = comfyui.wait(cfg, "p1", 60, lambda: False, clock.sleep, clock.now, alive=lambda: False)
    assert r["status"] == "error" and r["error"] == "comfyui process ended"
    assert r["outputs"] == [str(tmp_path / "ComfyUI" / "output" / "sub" / "a.png")]


def test_wait_reports_done_when_the_prompt_finished_before_the_process_ended(fake_comfy, clock):
    fake, cfg = fake_comfy
    fake.complete("p1", [("a.png", "", "output")])
    r = comfyui.wait(cfg, "p1", 60, lambda: False, clock.sleep, clock.now, alive=lambda: False)
    assert r["status"] == "done" and len(r["outputs"]) == 1


# --- free and stop ---------------------------------------------------------------------------


def test_free_asks_comfyui_to_unload_models_and_free_memory(fake_comfy):
    fake, cfg = fake_comfy
    assert comfyui.free(cfg) is True
    assert ("POST", "/free") in fake.calls
    assert json.loads(fake.last_body) == {"unload_models": True, "free_memory": True}


def test_free_is_false_when_nothing_answers(silent_cfg):
    assert comfyui.free(silent_cfg) is False


def test_delete_pending_posts_the_ids_to_the_queue(fake_comfy):
    fake, cfg = fake_comfy
    assert comfyui.delete_pending(cfg, ["p2", "p3"]) is True
    assert ("POST", "/queue") in fake.calls
    assert fake.queue_posts == [{"delete": ["p2", "p3"]}]


def test_delete_pending_sends_nothing_for_an_empty_list(fake_comfy):
    fake, cfg = fake_comfy
    assert comfyui.delete_pending(cfg, []) is True
    assert fake.calls == []


def test_delete_pending_is_false_when_nothing_answers(silent_cfg):
    assert comfyui.delete_pending(silent_cfg, ["p2"]) is False


STOP_CFG = ComfyCfg(dir=".", bat="x.bat", url="http://127.0.0.1:8188")


def forbidden_kill(pid, sig):
    pytest.fail(f"os.kill({pid}, {sig}) must not be called")


def test_stop_kills_listener_when_no_proc(monkeypatch, clock, lines):
    # The test the brief asks for, on Windows terms: no pid_alive faking, kill_tree(4242) is seen.
    asked, killed = [], []
    monkeypatch.setattr(procs, "IS_WINDOWS", True)
    monkeypatch.setattr(procs, "listener_pids", lambda port: asked.append(port) or [4242])
    monkeypatch.setattr(procs, "kill_tree", lambda pid: killed.append(pid) or True)
    monkeypatch.setattr(os, "kill", forbidden_kill)  # never touch a real pid
    r = comfyui.stop(STOP_CFG, None, lines, sleep=clock.sleep)
    assert r == {"stopped": True, "pids": [4242]}
    assert asked == [8188] and killed == [4242]
    assert clock.sleeps == []  # no 10 s grace: on Windows terminating would only orphan the children
    assert lines  # something was logged


def test_stop_off_windows_terminates_then_waits_10s_then_kills_the_tree(monkeypatch, clock, lines):
    events = []
    monkeypatch.setattr(procs, "IS_WINDOWS", False)
    monkeypatch.setattr(procs, "listener_pids", lambda port: [4242])
    monkeypatch.setattr(procs, "pid_alive", lambda pid: True)  # it ignores the terminate
    monkeypatch.setattr(procs, "kill_tree", lambda pid: events.append(("kill_tree", pid)) or True)
    monkeypatch.setattr(os, "kill", lambda pid, sig: events.append(("terminate", pid)))
    r = comfyui.stop(STOP_CFG, None, lines, sleep=clock.sleep)
    assert r == {"stopped": True, "pids": [4242]}
    assert events == [("terminate", 4242), ("kill_tree", 4242)]
    assert clock.sleeps == [0.5] * 20


def test_stop_off_windows_does_not_wait_when_the_process_exits_but_still_calls_kill_tree(
    monkeypatch, clock, lines
):
    gone, killed = [], []
    monkeypatch.setattr(procs, "IS_WINDOWS", False)
    monkeypatch.setattr(procs, "listener_pids", lambda port: [4242, 4243])
    monkeypatch.setattr(procs, "pid_alive", lambda pid: pid not in gone)
    monkeypatch.setattr(procs, "kill_tree", lambda pid: killed.append(pid) or True)
    monkeypatch.setattr(os, "kill", lambda pid, sig: gone.append(pid))
    r = comfyui.stop(STOP_CFG, None, lines, sleep=clock.sleep)
    assert r == {"stopped": True, "pids": [4242, 4243]} and clock.sleeps == []
    assert killed == [4242, 4243]  # kill_tree is not gated on pid_alive


@pytest.mark.parametrize("windows", [True, False])
def test_stop_reports_a_process_that_will_not_die(monkeypatch, clock, lines, windows):
    killed = []
    monkeypatch.setattr(procs, "IS_WINDOWS", windows)
    monkeypatch.setattr(procs, "listener_pids", lambda port: [4242, 4243])
    monkeypatch.setattr(procs, "pid_alive", lambda pid: True)
    monkeypatch.setattr(procs, "kill_tree", lambda pid: killed.append(pid) or pid == 4243)
    monkeypatch.setattr(os, "kill", lambda pid, sig: None)
    r = comfyui.stop(STOP_CFG, None, lines, sleep=clock.sleep)
    assert r == {"stopped": False, "pids": [4242, 4243]}
    assert killed == [4242, 4243]  # the second is still tried after the first fails


def test_stop_with_nothing_listening_does_nothing(monkeypatch, clock, lines):
    cfg = ComfyCfg(dir=".", bat="x.bat", url="http://127.0.0.1:8188")
    monkeypatch.setattr(procs, "listener_pids", lambda port: [])
    assert comfyui.stop(cfg, None, lines, sleep=clock.sleep) == {"stopped": False, "pids": []}
    assert clock.sleeps == []


def test_stop_terminates_the_given_process(lines):
    cfg = ComfyCfg(dir=".", bat="x.bat", url="http://127.0.0.1:1")
    proc = subprocess.Popen(
        [sys.executable, "-c", "import time; time.sleep(60)"],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    try:
        r = comfyui.stop(cfg, proc, lines)
        assert r == {"stopped": True, "pids": [proc.pid]}
        assert proc.poll() is not None
    finally:
        if proc.poll() is None:
            proc.kill()
        proc.wait(10)
