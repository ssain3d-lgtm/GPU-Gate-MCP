"""VoiceStudio adapter: start the TTS backend, run the user's batch script against it, stop it.

VoiceStudio is a local text-to-speech backend (a web server with GET /health). It is started with
the user's own start script (a PowerShell .ps1 that sets its environment and runs the server in the
foreground, or any other executable script). The work itself is done by the user's batch script,
`voice_batch.py <jobs.json>`: it posts every job to the backend, writes each job's `out` wav and
unloads the TTS model when it is done. This module does not talk to the synthesis endpoints.

Standard library only. `sleep` and `now` are injectable so tests never wait in real time; `log` is a
callable taking one line of text.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from typing import Callable

from . import http, procs
from .config import VoiceCfg

READY_POLL_S = 1.0  # how often wait_ready asks /health
BATCH_POLL_S = 0.5  # how often run_batch looks at the batch script
KILL_WAIT_S = 5.0  # how long run_batch waits for a killed batch script to be gone
JOBS_FILE = "voice_jobs.json"  # in work_dir: the jobs handed to the batch script
BATCH_LOG = "voice_batch.log"  # in work_dir: the batch script's stdout and stderr

Log = Callable[[str], None]


def _require(value: str | None, key: str) -> str:
    """A config value an operation needs; ValueError naming the key when it is not set."""
    if not value:
        raise ValueError(f"voicestudio.{key} is not set")
    return value


def ready(cfg: VoiceCfg) -> bool:
    """True when GET /health answers 200 with {"status": "ok"}."""
    code, body = http.http_json(cfg.url.rstrip("/") + "/health")
    return code == 200 and isinstance(body, dict) and body.get("status") == "ok"


def start(
    cfg: VoiceCfg, log_path: str, log: Log, bat_override: str | None = None
) -> subprocess.Popen:
    """Start the backend detached, its output appended to log_path, in cfg.dir.

    A .ps1 start script runs through `powershell -NoProfile -ExecutionPolicy Bypass -File`, any
    other script as it is. bat_override is accepted so every executor can be started the same way;
    VoiceStudio has no variants and ignores it.
    """
    script = _require(cfg.start_script, "start_script")
    if script.lower().endswith(".ps1"):
        argv = ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", script]
    else:
        argv = [script]
    log(f"voicestudio: starting {subprocess.list2cmdline(argv)}")
    return procs.spawn_detached(argv, cfg.dir, log_path)


def wait_ready(
    cfg: VoiceCfg,
    proc: subprocess.Popen,
    timeout_s: float,
    sleep: Callable[[float], None] = time.sleep,
    now: Callable[[], float] = time.monotonic,
) -> float | None:
    """Poll /health until the backend is ready; the seconds it took, or None when the process exits
    first or timeout_s passes (the caller tells them apart with proc.poll())."""
    t0 = now()
    while True:
        if ready(cfg):
            return now() - t0
        if proc.poll() is not None or now() - t0 >= timeout_s:
            return None
        sleep(READY_POLL_S)


def _job_result(
    index: int, job: dict, status: str, code: int | None, cwd: str, seconds: float
) -> dict:
    """One job's result. status is "timeout", "cancelled" or "exited" (the script ended by itself,
    with exit code `code`); an exited job is done only when the code is 0 and its out file exists."""
    result = {"index": index, "status": status, "seconds": seconds, "outputs": [], "error": None}
    if status != "exited":
        return result
    out = job.get("out")
    if code != 0:
        error = f"batch script exited {code}"
    elif not isinstance(out, str) or not out:
        error = "job has no out path"
    else:
        path = os.path.abspath(os.path.join(cwd, out))  # a relative out is relative to the script's cwd
        if os.path.isfile(path):
            result.update(status="done", outputs=[path])
            return result
        error = f"output missing: {out}"
    result.update(status="error", error=error)
    return result


def run_batch(
    cfg: VoiceCfg,
    jobs: list[dict],
    work_dir: str,
    timeout_s: float,
    cancelled: Callable[[], bool],
    log: Log,
    sleep: Callable[[float], None] = time.sleep,
    now: Callable[[], float] = time.monotonic,
) -> list[dict]:
    """Run the batch script over jobs and report each job.

    jobs go to <work_dir>/voice_jobs.json (UTF-8) and the script runs as
    `python <cfg.batch_script> <that file>` in cfg.dir (else work_dir), its output appended to
    <work_dir>/voice_batch.log. Its environment is ours plus VOICESTUDIO_URL=cfg.url and, when cfg.dir
    is set, VOICESTUDIO_ROOT=cfg.dir. work_dir must exist.

    The script is watched every 0.5 s until it exits, timeout_s passes or cancelled() turns true; the
    last two kill it and its process tree. One result per job, in order:
    {"index", "status", "seconds", "outputs", "error"}. status is "done" (the script exited 0 and
    the job's `out` file exists; outputs is its absolute path), "error" (a non-zero exit, `batch
    script exited <code>`, takes every job; else `output missing: <out>`), "timeout" or
    "cancelled" (every job; outputs stay empty even if some files were already written). The script
    reports no per-job timing, so seconds is the whole run's duration for every job.
    """
    script = _require(cfg.batch_script, "batch_script")
    jobs_path = os.path.join(work_dir, JOBS_FILE)
    with open(jobs_path, "w", encoding="utf-8") as f:
        json.dump(jobs, f, ensure_ascii=False)
    cwd = cfg.dir or work_dir
    env = dict(os.environ)
    env["VOICESTUDIO_URL"] = cfg.url
    if cfg.dir:
        env["VOICESTUDIO_ROOT"] = cfg.dir
    argv = [sys.executable, script, jobs_path]
    log_path = os.path.join(work_dir, BATCH_LOG)
    log(f"voicestudio: running {subprocess.list2cmdline(argv)} ({len(jobs)} jobs, log {log_path})")

    t0 = now()
    proc = procs.spawn_detached(argv, cwd, log_path, env=env)
    status, code = "exited", None
    try:
        while True:
            code = proc.poll()
            if code is not None:
                break
            if cancelled():
                status = "cancelled"
                break
            if now() - t0 >= timeout_s:
                status = "timeout"
                break
            sleep(BATCH_POLL_S)
    finally:
        if proc.poll() is None:  # timeout, cancel, or an exception in cancelled()
            if not procs.kill_tree(proc.pid):
                log(f"voicestudio: could not kill the batch script (pid {proc.pid})")
            try:
                proc.wait(KILL_WAIT_S)  # collect the exit status: no zombie, no ResourceWarning
            except subprocess.TimeoutExpired:
                pass
    seconds = now() - t0
    log(f"voicestudio: batch script {status if code is None else f'exited with code {code}'}")
    return [_job_result(i, job, status, code, cwd, seconds) for i, job in enumerate(jobs)]


def stop(
    cfg: VoiceCfg,
    proc: subprocess.Popen | None,
    log: Log,
    sleep: Callable[[float], None] | None = None,
) -> dict:
    """End the backend and everything it started.

    The process is proc when given, else whatever listens on the port of cfg.url. How it is ended
    (and how long it is given) is procs.stop_pids's rule.

    Returns {"stopped", "pids"}: pids are the processes aimed at (empty when nothing was running,
    which is not "stopped"); stopped is True when there was at least one and every one is gone.
    """
    pids = [proc.pid] if proc is not None else procs.listener_pids(procs.url_port(cfg.url))
    if not pids:
        log("voicestudio: nothing to stop")
        return {"stopped": False, "pids": []}
    log(f"voicestudio: stopping pids {pids}")
    stopped = procs.stop_pids(pids, sleep, proc)
    log("voicestudio: stopped" if stopped else f"voicestudio: could not stop pids {pids}")
    return {"stopped": stopped, "pids": pids}
