import json
import os
import subprocess
import sys
import time
from types import SimpleNamespace

import pytest

from fakes import FakeServer, FakeVoice
from gpu_gate import procs, voicestudio
from gpu_gate.config import VoiceCfg

# A stand-in for voice_batch.py: reads the jobs file, writes an empty wav for every job's `out`
# (except the index named by FAKE_FAIL_INDEX, and jobs that have none), dumps what it was started with next to the jobs
# file and exits with FAKE_EXIT (default 0).
FAKE_BATCH = """
import json, os, sys

jobs_path = sys.argv[1]
with open(jobs_path, encoding="utf-8") as f:
    jobs = json.load(f)
dump = {
    "url": os.environ.get("VOICESTUDIO_URL"),
    "root": os.environ.get("VOICESTUDIO_ROOT"),
    "cwd": os.getcwd(),
    "argv": sys.argv[1:],
}
with open(os.path.join(os.path.dirname(jobs_path), "env_dump.json"), "w", encoding="utf-8") as f:
    json.dump(dump, f)
for i, job in enumerate(jobs):
    if str(i) == os.environ.get("FAKE_FAIL_INDEX") or "out" not in job:
        continue
    out = os.path.abspath(job["out"])
    os.makedirs(os.path.dirname(out), exist_ok=True)
    open(out, "wb").close()
sys.exit(int(os.environ.get("FAKE_EXIT", "0")))
"""

# The same, but it only records its pid and then sleeps: a batch that never ends.
SLOW_BATCH = """
import os, sys, time

with open(os.path.join(os.path.dirname(sys.argv[1]), "slow.pid"), "w") as f:
    f.write(str(os.getpid()))
time.sleep(30)
"""


@pytest.fixture
def lines():
    return []


@pytest.fixture
def work_dir(tmp_path):
    path = tmp_path / "work"
    path.mkdir()
    return path


def make_cfg(tmp_path, script_source=FAKE_BATCH, **overrides):
    app = tmp_path / "app"
    app.mkdir(exist_ok=True)
    script = tmp_path / "batch_script.py"
    script.write_text(script_source, encoding="utf-8")
    values = {"dir": str(app), "batch_script": str(script), "url": "http://127.0.0.1:3900"}
    values.update(overrides)
    return VoiceCfg(**values)


@pytest.fixture
def cfg(tmp_path):
    return make_cfg(tmp_path)


def make_job(tmp_path, name, text="hello"):
    return {"text": text, "out": str(tmp_path / "wavs" / f"{name}.wav")}


def never():
    return False


class Mute(FakeServer):
    """Hangs up without answering: what a VoiceStudio that is not there looks like."""

    def handle(self, method, path, body):
        return None


# --- run_batch -----------------------------------------------------------------------------------


def test_run_batch_marks_each_job_by_its_output_file(tmp_path, work_dir, cfg, lines, monkeypatch):
    monkeypatch.setenv("FAKE_FAIL_INDEX", "1")
    jobs = [make_job(tmp_path, "a", "안녕하세요"), make_job(tmp_path, "b")]
    results = voicestudio.run_batch(cfg, jobs, str(work_dir), 60, never, lines.append)
    assert [r["index"] for r in results] == [0, 1]
    assert [r["status"] for r in results] == ["done", "error"]
    assert results[0]["outputs"] == [jobs[0]["out"]] and results[0]["error"] is None
    assert results[1]["outputs"] == [] and results[1]["error"] == f"output missing: {jobs[1]['out']}"
    assert results[0]["seconds"] == results[1]["seconds"] and results[0]["seconds"] >= 0
    jobs_file = work_dir / "voice_jobs.json"
    assert json.loads(jobs_file.read_text(encoding="utf-8")) == jobs
    assert "안녕하세요" in jobs_file.read_text(encoding="utf-8")  # kept as is, not \uXXXX escapes
    assert (work_dir / "voice_batch.log").exists()
    assert lines  # something was logged


def test_run_batch_resolves_a_relative_out_against_the_script_cwd(work_dir, cfg, lines):
    results = voicestudio.run_batch(
        cfg, [{"text": "x", "out": os.path.join("wavs", "rel.wav")}], str(work_dir), 60, never, lines.append
    )
    expected = os.path.join(cfg.dir, "wavs", "rel.wav")
    assert results[0]["status"] == "done" and results[0]["outputs"] == [expected]
    assert os.path.isfile(expected)


def test_run_batch_passes_url_and_root_env(work_dir, cfg, lines):
    voicestudio.run_batch(cfg, [], str(work_dir), 60, never, lines.append)
    dump = json.loads((work_dir / "env_dump.json").read_text())
    assert dump["url"] == cfg.url and dump["root"] == cfg.dir
    assert os.path.samefile(dump["cwd"], cfg.dir)
    assert dump["argv"] == [str(work_dir / "voice_jobs.json")]


def test_run_batch_without_dir_runs_in_work_dir_and_sets_no_root(
    tmp_path, work_dir, lines, monkeypatch
):
    monkeypatch.delenv("VOICESTUDIO_ROOT", raising=False)
    cfg = make_cfg(tmp_path, dir=None)
    voicestudio.run_batch(cfg, [], str(work_dir), 60, never, lines.append)
    dump = json.loads((work_dir / "env_dump.json").read_text())
    assert dump["root"] is None and os.path.samefile(dump["cwd"], work_dir)


def test_run_batch_nonzero_exit_fails_every_job_even_with_files_written(
    tmp_path, work_dir, cfg, lines, monkeypatch
):
    monkeypatch.setenv("FAKE_EXIT", "3")
    jobs = [make_job(tmp_path, "a"), make_job(tmp_path, "b")]
    results = voicestudio.run_batch(cfg, jobs, str(work_dir), 60, never, lines.append)
    assert [r["status"] for r in results] == ["error", "error"]
    assert {r["error"] for r in results} == {"batch script exited 3"}
    assert [r["outputs"] for r in results] == [[], []]


def test_run_batch_a_job_without_out_is_an_error_not_a_crash(tmp_path, work_dir, cfg, lines):
    jobs = [{"text": "no out"}, make_job(tmp_path, "b")]
    results = voicestudio.run_batch(cfg, jobs, str(work_dir), 60, never, lines.append)
    assert [r["status"] for r in results] == ["error", "done"]
    assert results[0]["error"] == "job has no out path"


def test_run_batch_timeout_kills_and_marks_timeout(tmp_path, work_dir, lines):
    cfg = make_cfg(tmp_path, SLOW_BATCH)
    pidfile = work_dir / "slow.pid"
    started = None

    def now():  # the clock starts once the script is up, so a slow interpreter start cannot fake a timeout
        nonlocal started
        if started is None and pidfile.exists():
            started = time.monotonic()
        return 0.0 if started is None else time.monotonic() - started

    jobs = [make_job(tmp_path, "a"), make_job(tmp_path, "b")]
    began = time.monotonic()
    try:
        results = voicestudio.run_batch(
            cfg, jobs, str(work_dir), 0.5, never, lines.append, sleep=lambda s: time.sleep(0.05), now=now
        )
        assert time.monotonic() - began < 10  # not the 30 s the script wanted
        assert [r["status"] for r in results] == ["timeout", "timeout"]
        assert [r["outputs"] for r in results] == [[], []]
        assert results[0]["seconds"] >= 0.5
        assert not procs.pid_alive(int(pidfile.read_text()))
    finally:
        if pidfile.exists():
            procs.kill_tree(int(pidfile.read_text()))


def test_run_batch_cancel_kills_and_marks_cancelled(tmp_path, work_dir, lines):
    cfg = make_cfg(tmp_path, SLOW_BATCH)
    pidfile = work_dir / "slow.pid"
    try:
        results = voicestudio.run_batch(
            cfg,
            [make_job(tmp_path, "a"), make_job(tmp_path, "b")],
            str(work_dir),
            600,
            pidfile.exists,  # cancel as soon as the script is up
            lines.append,
            sleep=lambda s: time.sleep(0.05),
        )
        assert [r["status"] for r in results] == ["cancelled", "cancelled"]
        assert [r["outputs"] for r in results] == [[], []]
        assert not procs.pid_alive(int(pidfile.read_text()))
    finally:
        if pidfile.exists():
            procs.kill_tree(int(pidfile.read_text()))


def test_run_batch_polls_every_half_second_until_the_timeout(tmp_path, work_dir, clock, lines):
    cfg = make_cfg(tmp_path, SLOW_BATCH)
    results = voicestudio.run_batch(
        cfg, [make_job(tmp_path, "a")], str(work_dir), 1.5, never, lines.append, clock.sleep, clock.now
    )
    assert results[0]["status"] == "timeout" and results[0]["seconds"] == 1.5
    assert clock.sleeps == [0.5, 0.5, 0.5]
    pidfile = work_dir / "slow.pid"
    if pidfile.exists():  # it may have been killed before it got that far
        assert not procs.pid_alive(int(pidfile.read_text()))


def test_run_batch_needs_the_batch_script(tmp_path, work_dir, lines):
    cfg = make_cfg(tmp_path, batch_script=None)
    with pytest.raises(ValueError, match=r"voicestudio\.batch_script is not set"):
        voicestudio.run_batch(cfg, [], str(work_dir), 60, never, lines.append)


# --- start and stop ------------------------------------------------------------------------------


def spawn_spy(monkeypatch):
    seen = []

    def fake_spawn(argv, cwd, log_path, env=None):
        seen.append({"argv": argv, "cwd": cwd, "log_path": log_path, "env": env})
        return "proc"

    monkeypatch.setattr(procs, "spawn_detached", fake_spawn)
    return seen


def test_start_uses_powershell_for_ps1(monkeypatch, lines):
    seen = spawn_spy(monkeypatch)
    cfg = VoiceCfg(dir="appdir", start_script="start_backend.ps1")
    assert voicestudio.start(cfg, "backend.log", lines.append) == "proc"
    assert seen == [
        {
            "argv": ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", "start_backend.ps1"],
            "cwd": "appdir",
            "log_path": "backend.log",
            "env": None,
        }
    ]
    assert lines == [
        "voicestudio: starting powershell -NoProfile -ExecutionPolicy Bypass -File start_backend.ps1"
    ]


@pytest.mark.parametrize(
    "script, argv",
    [
        ("START_BACKEND.PS1", ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", "START_BACKEND.PS1"]),
        ("start_backend.sh", ["start_backend.sh"]),
        ("start_backend.bat", ["start_backend.bat"]),
    ],
)
def test_start_runs_any_other_script_as_it_is(monkeypatch, lines, script, argv):
    seen = spawn_spy(monkeypatch)
    voicestudio.start(VoiceCfg(dir="appdir", start_script=script), "backend.log", lines.append)
    assert seen[0]["argv"] == argv


def test_start_accepts_and_ignores_bat_override(monkeypatch, lines):
    seen = spawn_spy(monkeypatch)
    cfg = VoiceCfg(dir="appdir", start_script="start_backend.sh")
    voicestudio.start(cfg, "backend.log", lines.append, bat_override="other.bat")
    assert seen[0]["argv"] == ["start_backend.sh"]


def test_start_needs_the_start_script(lines):
    with pytest.raises(ValueError, match=r"voicestudio\.start_script is not set"):
        voicestudio.start(VoiceCfg(dir="appdir"), "backend.log", lines.append)


def test_stop_hands_the_listener_on_the_url_port_to_stop_pids(monkeypatch, clock, lines):
    asked, handed = [], []
    monkeypatch.setattr(procs, "listener_pids", lambda port: asked.append(port) or [4242])
    monkeypatch.setattr(
        procs, "stop_pids", lambda pids, sleep=None, proc=None: handed.append((pids, sleep, proc)) or True
    )
    cfg = VoiceCfg(url="http://127.0.0.1:3900")
    assert voicestudio.stop(cfg, None, lines.append, sleep=clock.sleep) == {"stopped": True, "pids": [4242]}
    assert asked == [3900] and handed == [([4242], clock.sleep, None)]
    assert lines


def test_stop_hands_the_given_process_to_stop_pids(monkeypatch, lines):
    handed = []
    monkeypatch.setattr(procs, "listener_pids", lambda port: pytest.fail("the process is known"))
    monkeypatch.setattr(
        procs, "stop_pids", lambda pids, sleep=None, proc=None: handed.append((pids, proc)) or False
    )
    proc = SimpleNamespace(pid=77)
    assert voicestudio.stop(VoiceCfg(), proc, lines.append) == {"stopped": False, "pids": [77]}
    assert handed == [([77], proc)]


def test_stop_with_nothing_listening_does_nothing(monkeypatch, lines):
    monkeypatch.setattr(procs, "listener_pids", lambda port: [])
    monkeypatch.setattr(procs, "stop_pids", lambda *a, **k: pytest.fail("nothing to stop"))
    assert voicestudio.stop(VoiceCfg(), None, lines.append) == {"stopped": False, "pids": []}


def test_stop_terminates_the_given_process(lines):
    proc = subprocess.Popen(
        [sys.executable, "-c", "import time; time.sleep(60)"],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    try:
        r = voicestudio.stop(VoiceCfg(url="http://127.0.0.1:1"), proc, lines.append)
        assert r == {"stopped": True, "pids": [proc.pid]}
        assert proc.poll() is not None
    finally:
        if proc.poll() is None:
            proc.kill()
        proc.wait(10)


# --- ready and wait_ready ------------------------------------------------------------------------


def test_ready_is_true_when_health_says_ok(fake_voice):
    fake, cfg = fake_voice
    assert voicestudio.ready(cfg) is True
    assert ("GET", "/health") in fake.calls


def test_ready_is_false_when_health_says_anything_else(voice_factory):
    _, cfg = voice_factory(status="loading")
    assert voicestudio.ready(cfg) is False


def test_ready_is_false_when_nothing_answers():
    fake = Mute()
    try:
        assert voicestudio.ready(VoiceCfg(url=fake.start())) is False
    finally:
        fake.stop()


def test_wait_ready_polls_every_second_until_the_server_answers(monkeypatch, clock):
    answers = iter([False, False, True])
    monkeypatch.setattr(voicestudio, "ready", lambda cfg: next(answers))
    proc = SimpleNamespace(poll=lambda: None)
    assert voicestudio.wait_ready(VoiceCfg(), proc, 60, clock.sleep, clock.now) == 2.0
    assert clock.sleeps == [1.0, 1.0]


def test_wait_ready_gives_up_when_the_process_dies_first(monkeypatch, clock):
    monkeypatch.setattr(voicestudio, "ready", lambda cfg: False)
    proc = SimpleNamespace(poll=lambda: 1)
    assert voicestudio.wait_ready(VoiceCfg(), proc, 60, clock.sleep, clock.now) is None
    assert clock.sleeps == []


def test_wait_ready_gives_up_at_the_timeout(monkeypatch, clock):
    monkeypatch.setattr(voicestudio, "ready", lambda cfg: False)
    proc = SimpleNamespace(poll=lambda: None)
    assert voicestudio.wait_ready(VoiceCfg(), proc, 3, clock.sleep, clock.now) is None
    assert clock.sleeps == [1.0, 1.0, 1.0]
