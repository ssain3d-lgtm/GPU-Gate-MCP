"""The batch worker: one detached process runs one batch from start to end.

    0 check the jobs, the `comfy_variant` option and that no other LLM server is running
      (`options.force` overrides the last)
    1 pause Strata (unload its engine)
    2 start the executor (ComfyUI or VoiceStudio), or reuse the one that is already up (also one
      that holds its port but is still starting: it is waited for, never started a second time)
    3 run the jobs in order, in segments: consecutive workflows are queued together and then waited
      for one by one; a command runs once every earlier job has finished (VoiceStudio: one run of
      the batch script for all jobs)
    4 take unfinished prompts off the queue, free and stop the executor
    5 resume Strata
    6 write the result

Steps 4 and 5 run in `finally` blocks, so Strata is reloaded whatever happens in steps 2-3, even
after the client that asked for the batch has gone away. During step 3 Strata's /health is looked at
every 10 s: when the engine comes back (the user started a chat), the batch notes it once in
`warnings` and goes on without unloading it again.

Every external effect goes through `Deps`, so the tests script it; `default_deps` binds the real
adapters. Progress goes to the batch file and state.json at each transition. Results hold paths,
numbers and short strings only, never configuration or environment values.

Standard library only.
"""

from __future__ import annotations

import functools
import os
import socket
import subprocess
import sys
import time
import traceback
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

from . import comfyui, config, gpu, procs, state, strata, voicestudio

WATCH_S = 10  # how often step 3 looks at Strata's /health
VRAM_GONE_S = 60  # how long a stopped executor pid is given to release its VRAM
COMMAND_POLL_S = 0.5
READY_POLL_S = 1  # how often step 2 looks at an executor that someone else is starting
KILL_WAIT_S = 5.0
CONNECT_TIMEOUT_S = 1.0
MAX_ERROR_CHARS = 300
PAUSED = ("unloaded", "not loaded")

Log = Callable[[str], None]


@dataclass
class Deps:
    """Everything the worker does to the outside world. The executor fields act on the batch's kind."""

    strata_health: Callable[[], dict | None]
    strata_pause: Callable[[Log], dict]
    strata_resume: Callable[[Log], dict]
    exec_ready: Callable[[], bool]
    exec_port_busy: Callable[[], bool]  # something listens on the executor's port (it may be starting)
    exec_validate_variant: Callable[[Any], str | None]  # (comfy_variant) -> why it is refused, or None
    exec_start: Callable[..., Any]  # (log_path, log, bat_override=None) -> Popen
    exec_wait_ready: Callable[[Any, float], float | None]  # (proc, timeout_s)
    exec_submit: Callable[[dict, str], str]  # (workflow, client_id) -> prompt id
    exec_wait: Callable[..., dict]  # (prompt_id, timeout_s, cancelled, alive=None)
    exec_free: Callable[[], bool]
    exec_stop: Callable[[Any, Log], dict]  # (proc or None, log) -> {"stopped", "pids"}
    exec_load_workflow: Callable[[str], dict]
    exec_delete_pending: Callable[[list], bool]
    voice_run_batch: Callable[..., list]  # (jobs, work_dir, timeout_s, cancelled, log)
    run_command: Callable[..., tuple]  # (argv, cwd, timeout_s, cancelled, log_path) -> (code|None, s)
    vram_used: Callable[[], int | None]
    pid_vram_gone: Callable[[int, float], bool]  # (pid, timeout_s)
    other_llm_alive: Callable[[list], list]  # (ports) -> the ports that answer
    now: Callable[[], float]
    sleep: Callable[[float], None]


# --- the real dependencies ---------------------------------------------------------------------


def _tcp_alive(ports: list[int]) -> list[int]:
    """The ports on 127.0.0.1 that accept a TCP connection within 1 s."""
    alive = []
    for port in ports:
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=CONNECT_TIMEOUT_S):
                alive.append(port)
        except OSError:
            pass
    return alive


def _port_busy(url: str) -> bool:
    """True when a process listens on the TCP port of url (an executor that may still be starting)."""
    return bool(procs.listener_pids(procs.url_port(url)))


def _run_command(
    argv: list[str],
    cwd: str | None,
    timeout_s: float,
    cancelled: Callable[[], bool],
    log_path: str,
    sleep: Callable[[float], None] = time.sleep,
    now: Callable[[], float] = time.monotonic,
) -> tuple[int | None, float]:
    """Run argv in cwd, its output appended to log_path; (exit code, seconds).

    The code is None when the command was killed because timeout_s passed or cancelled() turned
    true. Raises OSError when the program cannot be started.
    """
    t0 = now()
    proc = procs.spawn_detached(argv, cwd, log_path)
    try:
        while True:
            code = proc.poll()
            if code is not None:
                return code, now() - t0
            if cancelled() or now() - t0 >= timeout_s:
                return None, now() - t0
            sleep(COMMAND_POLL_S)
    finally:
        if proc.poll() is None:
            procs.kill_tree(proc.pid)
            try:
                proc.wait(KILL_WAIT_S)
            except subprocess.TimeoutExpired:
                pass


def _nothing_to_free() -> bool:
    """VoiceStudio has no /free: its batch script unloads the TTS model itself."""
    return True


def _no_variants(name: object) -> None:
    """VoiceStudio has no start variants: nothing to refuse."""
    return None


def default_deps(cfg: config.Config, kind: str = "comfyui") -> Deps:
    """The real adapters, bound to cfg; the executor fields to ComfyUI or VoiceStudio by kind."""
    if kind == "comfyui":
        executor, ecfg = comfyui, cfg.comfyui
        free = functools.partial(comfyui.free, cfg.comfyui)
        validate_variant = functools.partial(comfyui.variant_error, cfg.comfyui)
    elif kind == "voicestudio":
        executor, ecfg = voicestudio, cfg.voicestudio
        free = _nothing_to_free
        validate_variant = _no_variants
    else:
        raise ValueError(f"unknown batch kind: {kind!r}")
    sleep, now = time.sleep, time.monotonic
    clock = {"sleep": sleep, "now": now}
    return Deps(
        strata_health=functools.partial(strata.health, cfg.strata),
        strata_pause=functools.partial(strata.pause, cfg.strata, **clock),
        strata_resume=functools.partial(strata.resume, cfg.strata, **clock),
        exec_ready=functools.partial(executor.ready, ecfg),
        exec_port_busy=functools.partial(_port_busy, ecfg.url),
        exec_validate_variant=validate_variant,
        exec_start=functools.partial(executor.start, ecfg),
        exec_wait_ready=functools.partial(executor.wait_ready, ecfg, **clock),
        exec_submit=functools.partial(comfyui.submit, cfg.comfyui),
        exec_wait=functools.partial(comfyui.wait, cfg.comfyui, **clock),
        exec_free=free,
        exec_stop=functools.partial(executor.stop, ecfg, sleep=sleep),
        exec_load_workflow=comfyui.load_workflow,
        exec_delete_pending=functools.partial(comfyui.delete_pending, cfg.comfyui),
        voice_run_batch=functools.partial(voicestudio.run_batch, cfg.voicestudio, **clock),
        run_command=functools.partial(_run_command, **clock),
        vram_used=gpu.vram_used_mib,
        pid_vram_gone=functools.partial(gpu.pid_vram_gone, **clock),
        other_llm_alive=_tcp_alive,
        now=now,
        sleep=sleep,
    )


# --- results -----------------------------------------------------------------------------------


def _one_line(text: str) -> str:
    text = " ".join(str(text).split())
    return text if len(text) <= MAX_ERROR_CHARS else text[: MAX_ERROR_CHARS - 3] + "..."


def _describe(err: BaseException) -> str:
    """'<Type>: <message>' on one line."""
    return _one_line(f"{type(err).__name__}: {err}")


def _job_entry(index: int, job: object, batch_kind: str) -> dict:
    """A job's result before it runs; status None until it is decided."""
    entry = {
        "index": index, "kind": None, "name": None, "status": None,
        "seconds": None, "outputs": [], "error": None,
    }
    if not isinstance(job, dict):
        entry["name"] = f"job {index}"
        return entry
    if batch_kind == "voicestudio":
        out = job.get("out")
        entry.update(kind="voice", name=os.path.basename(out) if isinstance(out, str) and out else f"job {index}")
        return entry
    kind = job.get("kind")
    entry["kind"] = kind if isinstance(kind, str) else None
    name = job.get("name")
    if not (isinstance(name, str) and name):
        if kind == "workflow" and isinstance(job.get("path"), str):
            name = os.path.basename(job["path"])
        elif kind == "command" and isinstance(job.get("command"), list) and job["command"]:
            name = os.path.basename(str(job["command"][0]))
        else:
            name = f"job {index}"
    entry["name"] = name
    return entry


def _precheck(job: object, batch_kind: str) -> str | None:
    """Why a job cannot run at all (it fails on its own, the others go on), or None."""
    if not isinstance(job, dict):
        return "job must be an object"
    if batch_kind == "voicestudio":
        return None
    kind = job.get("kind")
    if kind == "workflow":
        path = job.get("path")
        if not (isinstance(path, str) and path and os.path.isfile(path)):
            return f"workflow file not found: {path}"
        return None
    if kind == "command":
        argv = job.get("command")
        if not (isinstance(argv, list) and argv and all(isinstance(a, str) and a for a in argv)):
            return "command must be a non-empty list of strings"
        cwd = job.get("cwd")
        if cwd is not None and not (isinstance(cwd, str) and os.path.isdir(cwd)):
            return f"cwd not found: {cwd}"
        timeout = job.get("timeout_s")
        if timeout is not None and (isinstance(timeout, bool) or not isinstance(timeout, (int, float)) or timeout <= 0):
            return f"timeout_s must be a positive number: {timeout!r}"
        return None
    return f"unknown kind: {kind!r}"


def _skeleton(home: Path, batch: dict) -> dict:
    """The result before anything has run: every job undecided, nothing known about Strata."""
    batch_id = batch["batch_id"]
    kind = batch.get("kind") or "comfyui"
    jobs = batch.get("jobs") or []
    return {
        "batch_id": batch_id,
        "started": state.now_iso(),
        "finished": None,
        "ok": False,
        "strata": {"server": None, "paused_s": None, "restored": None, "load_s": None},
        "executor": {"kind": kind, "started_by_gate": False, "cold_start_s": None, "stopped": False},
        "jobs": [_job_entry(i, job, kind) for i, job in enumerate(jobs)],
        "warnings": list(batch.get("warnings") or []),  # the server may have noted something already
        "log": str(state.log_path(home, batch_id)),
        "error": None,
    }


def _progress(result: dict, current: str | None) -> dict:
    jobs = result["jobs"]
    return {"done": sum(1 for j in jobs if j["status"]), "total": len(jobs), "current": current}


def _write_final(home: Path, doc: dict, result: dict) -> None:
    """Close the result (undecided jobs become skipped, ok, finished) and write it everywhere."""
    for job in result["jobs"]:
        if job["status"] is None:
            job["status"] = "skipped"
    strata_ok = result["strata"]["server"] is False or result["strata"]["restored"] is True
    result["ok"] = (
        bool(result["jobs"])
        and all(j["status"] == "done" for j in result["jobs"])
        and strata_ok
        and result["error"] is None
    )
    result["finished"] = state.now_iso()
    final = "done" if result["ok"] else "failed"
    doc.update(
        state=final, result=result, finished=result["finished"],
        progress=_progress(result, None), warnings=result["warnings"],
    )
    state.write_json(state.batch_path(home, result["batch_id"]), doc)
    state.set_state(home, final, result["batch_id"], last_result=result)


# --- the run -----------------------------------------------------------------------------------


class _Run:
    """One batch: the steps share the result being built, the batch file and the watch."""

    def __init__(self, home: Path, batch: dict, cfg: config.Config, deps: Deps, log: Log):
        self.home = Path(home)
        self.cfg = cfg
        self.deps = deps
        self.log = log
        self.batch_id = batch["batch_id"]
        self.kind = batch.get("kind") or "comfyui"
        self.jobs = batch.get("jobs") or []
        self.options = batch.get("options") or {}
        self.result = _skeleton(self.home, batch)
        self.entries = self.result["jobs"]
        self.warnings = self.result["warnings"]
        self.doc = dict(batch)
        self.log_path = str(state.log_path(self.home, self.batch_id))
        self.t0 = deps.now()
        limits = cfg.limits
        self.job_timeout = self.options.get("job_timeout_s", limits.job_timeout_s)
        self.deadline = self.t0 + self.options.get("batch_timeout_s", limits.batch_timeout_s)
        self.proc = None  # the executor process, when the gate started it
        self.executor_up = False  # something to free and stop in step 4
        self.resume_needed = False
        self.current: int | None = None  # the index of the job being run
        self.prompts: dict[int, str] = {}  # job index -> ComfyUI prompt id
        self.finished: set[int] = set()  # jobs whose prompt ComfyUI is done with (or interrupted)
        self.dropped: set[int] = set()  # jobs whose prompt was taken off the queue
        # the watch (step 3)
        self.watching = False
        self.watch_last = 0.0
        self.came_back = False
        self.cancelled = False

    # -- bookkeeping --

    def _save(self, current: str | None = None) -> None:
        """Mirror the progress into the batch file. A failed write is logged, never fatal: it must
        not keep the executor running or Strata unloaded."""
        self.doc.update(progress=_progress(self.result, current), warnings=self.warnings)
        try:
            state.write_json(state.batch_path(self.home, self.batch_id), self.doc)
        except OSError as err:
            self.log(f"worker: could not write the batch file: {err}")

    def _transition(self, name: str) -> None:
        self.log(f"worker: {name}")
        self.doc["state"] = name
        self._save()
        try:
            state.set_state(self.home, name, self.batch_id)
        except OSError as err:
            self.log(f"worker: could not write state.json: {err}")

    def _fail(self, message: str) -> None:
        """Record the batch's error (the first one wins; later ones go to the log only)."""
        self.log(f"worker: error: {message}")
        if self.result["error"] is None:
            self.result["error"] = _one_line(message)

    def _skip_undecided(self) -> None:
        for entry in self.entries:
            if entry["status"] is None:
                entry["status"] = "skipped"

    def _guarded(self, what: str, fn: Callable[[], Any], default: Any) -> Any:
        """fn(), or default when it raises (the traceback goes to the log, a warning to the result)."""
        try:
            return fn()
        except Exception as err:
            self.log(f"worker: {what} failed\n{traceback.format_exc()}")
            self.warnings.append(_one_line(f"{what} failed: {_describe(err)}"))
            return default

    def _log_vram(self, when: str) -> None:
        used = self._guarded("VRAM reading", self.deps.vram_used, None)
        self.log(f"worker: VRAM in use {when}: {'unknown' if used is None else f'{used} MiB'}")

    # -- the probe handed to every wait in step 3: engine watch + cancel flag --

    def probe(self) -> bool:
        """True once the batch is cancelled. Also looks at Strata every WATCH_S seconds."""
        if self.watching and not self.came_back and self.deps.now() - self.watch_last >= WATCH_S:
            self.watch_last = self.deps.now()
            health = self.deps.strata_health()
            if health is not None and health.get("loaded"):
                self.came_back = True
                warning = f"LLM came back during the batch at {state.now_iso()}"
                self.log(f"worker: {warning}; not unloading it again")
                self.warnings.append(warning)
                self._save()
        if not self.cancelled and state.is_cancelled(self.home, self.batch_id):
            self.cancelled = True
            self.log("worker: cancel requested")
            self._drop_prompts(i for i in self.prompts if i >= (self.current or 0))
        return self.cancelled

    def _drop_prompts(self, indices) -> list[str]:
        """Take the prompts of these jobs off ComfyUI's queue, once each (a prompt that is already
        running is not affected); the ids sent."""
        wanted = sorted(i for i in indices if i in self.prompts and i not in self.dropped)
        ids = [self.prompts[i] for i in wanted]
        if ids:
            self.dropped.update(wanted)
            self.deps.exec_delete_pending(ids)
        return ids

    # -- steps --

    def execute(self) -> dict:
        if not self.jobs:
            raise ValueError("jobs is empty")
        for entry, job in zip(self.entries, self.jobs):
            reason = _precheck(job, self.kind)
            if reason:
                entry.update(status="error", error=reason)
        state.write_lock(self.home, self.batch_id, os.getpid())
        self.doc.update(pid=os.getpid(), started=self.result["started"], finished=None, result=None)

        if self._variant_refused() or self._other_llm_blocks():
            self._skip_undecided()
            return self._finish()
        try:
            go = self._pause()
            if go:
                try:
                    self._transition("starting")
                    if self._start_executor():
                        self._transition("running")
                        self._run_jobs()
                except Exception as err:
                    self.log(f"worker: batch failed\n{traceback.format_exc()}")
                    self._fail(_describe(err))
                finally:
                    self.watching = False
                    self._skip_undecided()
                    self._transition("cleanup")
                    self._cleanup()
        finally:
            if self.resume_needed:
                self._transition("restoring")
                self._resume()
        return self._finish()

    def _variant_refused(self) -> bool:
        """Step 0: a comfy_variant that step 2 would refuse ends the batch before Strata is unloaded
        for nothing. An empty one means the configured bat, as in step 2."""
        variant = self.options.get("comfy_variant")
        if self.kind != "comfyui" or not variant:
            return False
        problem = self.deps.exec_validate_variant(variant)
        if problem is None:
            return False
        self._fail(problem)
        return True

    def _other_llm_blocks(self) -> bool:
        ports = self.deps.other_llm_alive(list(self.cfg.other_llm_ports))
        if not ports:
            return False
        if self.options.get("force"):
            self.warnings.append(f"other LLM server answering on ports {ports}; going on because force is set")
            self.log(f"worker: {self.warnings[-1]}")
            return False
        self._fail(f"other_llm_running: ports {ports}")
        return True

    def _pause(self) -> bool:
        """Step 1. True when the batch may go on."""
        self._transition("pausing")
        health = self.deps.strata_health()
        seen = "no answer" if health is None else f"loaded={health.get('loaded')}"
        self.log(f"worker: strata health before the pause: {seen}")
        try:
            paused = self.deps.strata_pause(self.log)
        except Exception as err:
            self.log(f"worker: strata pause raised\n{traceback.format_exc()}")
            paused = {"status": "error", "error": _describe(err), "paused_s": None}
        status = paused.get("status")
        strata_result = self.result["strata"]
        strata_result["paused_s"] = paused.get("paused_s")
        if status == "absent":
            strata_result["server"] = False
            self.log("worker: no Strata server; nothing to pause or restore")
            return True
        strata_result["server"] = True
        if status in PAUSED:
            self.resume_needed = True
            self.watching, self.watch_last = True, self.deps.now()
            self._log_vram("after the pause")
            return True
        # busy: the unload was refused for 15 minutes, the engine is as it was; leave it alone.
        # error (or anything else): the engine may or may not be down, and resume is a no-op on a
        # loaded engine, so it is restored to be safe.
        self.resume_needed = status != "busy"
        detail = paused.get("error")
        self._fail(f"strata pause failed: {status}" + (f": {detail}" if detail else ""))
        self._skip_undecided()
        return False

    def _executor_cfg(self):
        return self.cfg.comfyui if self.kind == "comfyui" else self.cfg.voicestudio

    def _port(self) -> int:
        return procs.url_port(self._executor_cfg().url)

    def _ready_within(self, timeout: float) -> bool:
        """Look at the executor once a second until it answers; False when timeout passes first."""
        t0 = self.deps.now()
        while self.deps.now() - t0 < timeout:
            self.deps.sleep(READY_POLL_S)
            if self.deps.exec_ready():
                return True
        return False

    def _reuse_external(self, warning: str) -> bool:
        """Use an executor someone else started: step 4 treats it as external (stopped by its port
        unless keep_executor, never by a pid of the gate's)."""
        self.log(f"worker: {warning}")
        self.warnings.append(warning)
        self.proc = None
        self.executor_up = True
        self.result["executor"]["started_by_gate"] = False
        return True

    def _start_executor(self) -> bool:
        """Step 2. True when the executor is up."""
        executor = self.result["executor"]
        timeout = self._executor_cfg().start_timeout_s
        if self.deps.exec_ready():
            self.log(f"worker: {self.kind} is already running; using it")
            self.executor_up = True
            return True
        if self.deps.exec_port_busy():
            # Something holds the port but does not answer yet: an executor started from outside a
            # moment ago. A second one on the same port would be the gate's to stop, not that one.
            port = self._port()
            self.log(f"worker: port {port} is in use but {self.kind} does not answer yet; waiting for it")
            if self._ready_within(timeout):
                return self._reuse_external(f"executor was already starting on port {port}; reused it")
            self._fail(f"{self.kind} on port {port} (started outside the gate) did not answer within {timeout} s")
            self._skip_undecided()
            return False
        variant = self.options.get("comfy_variant") if self.kind == "comfyui" else None
        try:
            self.proc = self.deps.exec_start(self.log_path, self.log, bat_override=variant)
        except (ValueError, OSError) as err:
            self._fail(_one_line(str(err)) if isinstance(err, ValueError) else _describe(err))
            self._skip_undecided()
            return False
        self.executor_up = True
        executor["started_by_gate"] = True
        t0 = self.deps.now()
        cold = self.deps.exec_wait_ready(self.proc, timeout)
        if cold is None:
            code = self.proc.poll()
            if code is not None:
                # The gate's process ended first: when another one holds the port (it was started
                # from outside in the meantime), that one is used instead.
                left = max(0.0, timeout - (self.deps.now() - t0))
                if self.deps.exec_ready() or (self.deps.exec_port_busy() and self._ready_within(left)):
                    return self._reuse_external(
                        f"{self.kind} started by the gate exited with code {code}, but another one "
                        f"answers on port {self._port()}; reused it"
                    )
                self._fail(f"{self.kind} exited with code {code} before it was ready")
            else:
                self._fail(f"{self.kind} not ready after {timeout} s")
            self._skip_undecided()
            return False
        executor["cold_start_s"] = cold
        self.log(f"worker: {self.kind} ready after {cold:.0f} s")
        return True

    def _alive(self) -> Callable[[], bool] | None:
        proc = self.proc
        return None if proc is None else (lambda: proc.poll() is None)

    def _run_jobs(self) -> None:
        """Step 3."""
        if self.kind == "voicestudio":
            self._run_voice()
        else:
            self._run_comfy()

    def _stop_on_error(self) -> bool:
        return bool(self.options.get("stop_on_error"))

    def _is_workflow(self, i: int) -> bool:
        job = self.jobs[i]
        return isinstance(job, dict) and job.get("kind") == "workflow"

    def _run_comfy(self) -> None:
        """Jobs in order, in segments: a run of consecutive workflows is queued together (so ComfyUI
        never idles between them) and then waited for in order; any other job (a command) runs on
        its own once every earlier job has finished. A workflow after a command is queued only after
        that command is done."""
        n, i = len(self.jobs), 0
        while i < n:
            end = i + 1
            if self._is_workflow(i):
                while end < n and self._is_workflow(end):
                    end += 1
                if not self.cancelled and self.deps.now() < self.deadline:
                    self._submit_workflows(range(i, end))
            for k in range(i, end):
                if self._run_one(k):
                    self.log("worker: stop_on_error: skipping the rest")
                    self._drop_prompts(range(k + 1, end))
                    self._skip_undecided()
                    return
            i = end

    def _submit_workflows(self, indices: range) -> None:
        client_id = f"gpu-gate-{self.batch_id}"
        for i in indices:
            entry, job = self.entries[i], self.jobs[i]
            if self._stop_on_error() and any(e["status"] == "error" for e in self.entries[:i]):
                return  # these jobs will be skipped: do not queue them
            if entry["status"] is not None:
                continue
            try:
                workflow = self.deps.exec_load_workflow(job["path"])
            except (OSError, ValueError) as err:
                entry.update(status="error", error=_one_line(str(err)))
                continue
            try:
                self.prompts[i] = self.deps.exec_submit(workflow, client_id)
            except comfyui.SubmitError as err:
                entry.update(status="error", error=_one_line(str(err)))
                continue
            self.log(f"worker: queued {entry['name']} as {self.prompts[i]}")

    def _run_one(self, i: int) -> bool:
        """Wait for job i's prompt or run its command, unless it is already decided, cancelled or
        out of time. True when the batch must stop here (an error under stop_on_error)."""
        entry, job = self.entries[i], self.jobs[i]
        if entry["status"] is None:
            remaining = self.deadline - self.deps.now()
            if self.cancelled:
                entry["status"] = "cancelled"
            elif remaining <= 0:
                entry.update(status="timeout", error="batch timeout")
            else:
                self.current = i
                self._save(entry["name"])
                if self._is_workflow(i):
                    self._wait_workflow(i, entry, remaining)
                else:
                    self._run_command(entry, job, remaining)
                self.current = None
                self.log(f"worker: {entry['name']}: {entry['status']}" + (f" ({entry['error']})" if entry["error"] else ""))
                self._save()
        return entry["status"] == "error" and self._stop_on_error()

    def _wait_workflow(self, i: int, entry: dict, remaining: float) -> None:
        answer = self.deps.exec_wait(
            self.prompts[i], min(self.job_timeout, remaining), self.probe, alive=self._alive()
        )
        status = answer.get("status")
        if status in ("done", "error", "cancelled"):  # a timeout leaves the prompt in ComfyUI
            self.finished.add(i)
        if status == "timeout" and not answer.get("error"):
            why = "batch timeout" if remaining < self.job_timeout else f"job timeout ({self.job_timeout} s)"
            answer = {**answer, "error": why}
        entry.update(
            status=status,
            seconds=answer.get("seconds"),
            outputs=list(answer.get("outputs") or []),
            error=answer.get("error"),
        )

    def _run_command(self, entry: dict, job: dict, remaining: float) -> None:
        timeout = min(job.get("timeout_s") or self.job_timeout, remaining)
        try:
            code, seconds = self.deps.run_command(job["command"], job.get("cwd"), timeout, self.probe, self.log_path)
        except OSError as err:
            entry.update(status="error", error=_describe(err))
            return
        entry["seconds"] = seconds
        if code is None:
            entry["status"] = "cancelled" if self.cancelled else "timeout"
        elif code == 0:
            entry["status"] = "done"
        else:
            entry.update(status="error", error=f"exited {code}")

    def _run_voice(self) -> None:
        pending = [i for i, entry in enumerate(self.entries) if entry["status"] is None]
        if not pending:
            return
        remaining = self.deadline - self.deps.now()
        if remaining <= 0:
            for i in pending:
                self.entries[i].update(status="timeout", error="batch timeout")
            return
        work_dir = state.paths(self.home).batches_dir / self.batch_id
        work_dir.mkdir(parents=True, exist_ok=True)
        self._save("voice batch")
        answers = self.deps.voice_run_batch(
            [self.jobs[i] for i in pending], str(work_dir), remaining, self.probe, self.log
        )
        for i, answer in zip(pending, answers):
            self.entries[i].update(
                status=answer.get("status"),
                seconds=answer.get("seconds"),
                outputs=list(answer.get("outputs") or []),
                error=answer.get("error"),
            )
        self._save()

    def _cleanup(self) -> None:
        """Step 4."""
        executor = self.result["executor"]
        if not self.executor_up:
            return
        keep = bool(self.options.get("keep_executor")) and not executor["started_by_gate"]
        # Prompts left by a timeout, stop_on_error or an exception must not go on running (in a
        # kept executor) while Strata loads again: take the queued ones off the queue first.
        unfinished = [i for i in self.prompts if i not in self.finished and i not in self.dropped]
        self._guarded("queue cleanup", lambda: self._drop_prompts(unfinished), [])
        if keep and unfinished:
            # The adapter has no interrupt of its own outside a wait (and /interrupt is not scoped
            # to a prompt, so it could stop the user's own work in a kept ComfyUI).
            running = [self.prompts[i] for i in unfinished]
            self.warnings.append(
                f"kept {self.kind}: unfinished prompts {running} were taken off the queue; "
                "one that had already started may still be running"
            )
        self._guarded(f"{self.kind} free", self.deps.exec_free, False)
        if keep:
            self.log(f"worker: keep_executor: leaving the running {self.kind} up")
            return
        stopped = self._guarded(
            f"{self.kind} stop", lambda: self.deps.exec_stop(self.proc, self.log), {"stopped": False, "pids": []}
        )
        executor["stopped"] = bool(stopped.get("stopped"))
        pids = list(stopped.get("pids") or [])
        if pids and not executor["stopped"]:
            self.warnings.append(f"{self.kind} could not be stopped (pids {pids})")
        for pid in pids:
            gone = self._guarded("VRAM check", lambda: self.deps.pid_vram_gone(pid, VRAM_GONE_S), False)
            if not gone:
                self.warnings.append(f"pid {pid} still holds VRAM {VRAM_GONE_S} s after the stop")
        self._log_vram("after the cleanup")

    def _resume(self) -> None:
        """Step 5."""
        answer = self._guarded(
            "strata resume",
            lambda: self.deps.strata_resume(self.log),
            {"restored": False, "load_s": None, "error": "resume raised an exception"},
        )
        strata_result = self.result["strata"]
        strata_result["restored"] = bool(answer.get("restored"))
        strata_result["load_s"] = answer.get("load_s")
        if not strata_result["restored"]:
            self._fail(f"strata resume failed: {answer.get('error') or 'unknown'}")

    def _finish(self) -> dict:
        """Step 6."""
        _write_final(self.home, self.doc, self.result)
        self.log(f"worker: finished, ok={self.result['ok']}")
        return self.result


def run_batch(home: Path, batch: dict, cfg: config.Config, deps: Deps, log: Log) -> dict:
    """Run one batch (steps 0-6) and return its result; the lock is left for the caller to release.

    Raises ValueError for a batch without jobs, before anything is touched.
    """
    return _Run(home, batch, cfg, deps, log).execute()


# --- the process entry point -------------------------------------------------------------------


def _file_log(path: Path) -> Log:
    def log(message: str) -> None:
        try:
            with open(path, "a", encoding="utf-8") as f:
                f.write(f"{state.now_iso()} {message}\n")
        except OSError:
            pass  # a log that cannot be written must not stop the batch

    return log


def _release_own_lock(home: Path, batch_id: str) -> None:
    """Remove the lock, unless another batch holds it by now."""
    lock = state.read_json(state.paths(home).lock)
    if lock is None or lock.get("batch_id") == batch_id:
        state.release_lock(home)


def _write_failure(home: Path, batch: dict, message: str) -> None:
    """A failed result made of the message alone, unless the batch file already holds a real one
    (run_batch got as far as writing it): that one has the job results and the restore status."""
    written = state.read_json(state.batch_path(home, batch["batch_id"]))
    if written is not None and isinstance(written.get("result"), dict):
        return
    result = _skeleton(home, batch)
    result["error"] = _one_line(message)
    _write_final(home, dict(batch), result)


def main(batch_path: str) -> int:
    """Run the batch file at batch_path; 0 when the batch is ok, else 1. The lock is released
    (when it is this batch's) however the run ends."""
    home = state.home()
    batch = state.read_json(batch_path)
    batch_id = batch.get("batch_id") if batch is not None else None
    if not isinstance(batch_id, str):
        batch_id = Path(batch_path).stem  # the server names batch files <batch_id>.json
    try:
        if batch is None or not isinstance(batch.get("batch_id"), str):
            print(f"gpu-gate worker: not a batch file: {batch_path}", file=sys.stderr)
            return 1
        log = _file_log(state.log_path(home, batch_id))
        try:
            cfg = config.load(home)
            deps = default_deps(cfg, batch.get("kind") or "comfyui")
        except (config.ConfigError, ValueError) as err:
            log(f"worker: {err}")
            _write_failure(home, batch, str(err))
            return 1
        try:
            result = run_batch(home, batch, cfg, deps, log)
        except Exception as err:
            log(f"worker: batch failed\n{traceback.format_exc()}")
            _write_failure(home, batch, _describe(err))
            return 1
        return 0 if result["ok"] else 1
    finally:
        _release_own_lock(home, batch_id)
