"""Fake servers and a fake clock for the adapter tests.

Each fake is a real HTTP server on 127.0.0.1 with an ephemeral port, so the adapters are tested over
real sockets. They share FakeServer (start/stop, call recording, JSON replies); a subclass only
implements handle(method, path, body) -> (status, payload).
"""

from __future__ import annotations

import json
import os
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer


class Headers(dict):
    """The request headers as a plain dict whose lookups ignore case (urllib sends 'Content-type')."""

    def __init__(self, items=()):
        super().__init__((str(k).lower(), v) for k, v in items)

    def __getitem__(self, key):
        return super().__getitem__(str(key).lower())

    def __contains__(self, key):
        return super().__contains__(str(key).lower())

    def get(self, key, default=None):
        return super().get(str(key).lower(), default)


class FakeClock:
    """Injected as `now` and `sleep`: sleeping advances the clock instead of waiting."""

    def __init__(self, start: float = 0.0):
        self.t = start
        self.sleeps: list[float] = []

    def now(self) -> float:
        return self.t

    def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.t += seconds


class _Handler(BaseHTTPRequestHandler):
    def log_message(self, format, *args):  # keep the test output quiet
        pass

    def _serve(self, method: str) -> None:
        length = int(self.headers.get("Content-Length") or 0)
        body = self.rfile.read(length) if length else b""
        answer = self.server.owner._record_and_handle(
            method, self.path, Headers(self.headers.items()), body
        )
        if answer is None:  # drop the connection without a response
            self.close_connection = True
            return
        status, payload = answer
        data = payload if isinstance(payload, bytes) else json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        self._serve("GET")

    def do_POST(self):
        self._serve("POST")


class FakeServer:
    """Base: start() returns the base URL, stop() shuts down; .calls is [(method, path)]."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, str]] = []
        self.headers: Headers = Headers()  # the last request's headers
        self.last_body: bytes = b""  # the last request's body
        self._lock = threading.Lock()
        self._httpd: ThreadingHTTPServer | None = None
        self._thread: threading.Thread | None = None

    def start(self) -> str:
        self._httpd = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
        self._httpd.owner = self
        self._thread = threading.Thread(
            target=self._httpd.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True
        )
        self._thread.start()
        return f"http://127.0.0.1:{self._httpd.server_address[1]}"

    def stop(self) -> None:
        if self._httpd is not None:
            self._httpd.shutdown()
            self._httpd.server_close()
            self._httpd = None
        if self._thread is not None:
            self._thread.join(5)
            self._thread = None

    def _record_and_handle(self, method, path, headers, body):
        with self._lock:
            self.calls.append((method, path))
            self.headers = headers
            self.last_body = body
            return self.handle(method, path, body)

    def handle(self, method: str, path: str, body: bytes):
        """Return (status, payload), or None to drop the connection without an answer.

        Called with the lock held. A bytes payload is sent as is.
        """
        return 404, {"error": f"no route: {method} {path}"}


class FakeStrata(FakeServer):
    """Mirrors the real Strata endpoints the adapter uses.

    busy_times: the first N /unload calls answer 409 {"status": "busy"}.
    loaded: whether the engine starts loaded.
    load_fails: /load answers 503 instead of loading.
    unload_status: when set, /unload answers this HTTP code with an error body (a wrong key, a crash),
        after any busy_times 409s.
    unload_drop: /unload drops the connection without answering (a timeout or a crashed request).
    health_drop_after_unload: after an acknowledged /unload, this many /health calls are dropped
        without an answer before it recovers.
    health_lag: after an /unload or /load, /health keeps reporting the old `loaded` value for this
        many calls (the engine takes a moment to go away or come up); `.loaded` is the real state.
    absent: nothing is listening; start() starts no server and returns a URL nobody answers on.
    """

    DEAD_URL = "http://127.0.0.1:1"

    def __init__(
        self,
        busy_times: int = 0,
        absent: bool = False,
        loaded: bool = True,
        load_fails: bool = False,
        health_lag: int = 0,
        unload_status: int | None = None,
        unload_drop: bool = False,
        health_drop_after_unload: int = 0,
    ) -> None:
        super().__init__()
        self.busy_times = busy_times
        self.absent = absent
        self.loaded = loaded
        self.load_fails = load_fails
        self.health_lag = health_lag
        self.unload_status = unload_status
        self.unload_drop = unload_drop
        self.health_drop_after_unload = health_drop_after_unload
        self._health_drops_left = 0
        self._stale = loaded
        self._lag_left = 0

    def start(self) -> str:
        return self.DEAD_URL if self.absent else super().start()

    def _transition(self, loaded: bool) -> None:
        if loaded != self.loaded:
            self._stale = self.loaded
            self._lag_left = self.health_lag
        self.loaded = loaded

    def _reported_loaded(self) -> bool:
        if self._lag_left > 0:
            self._lag_left -= 1
            return self._stale
        return self.loaded

    def handle(self, method, path, body):
        route = path.split("?", 1)[0]
        if method == "GET" and route == "/health":
            if self._health_drops_left > 0:
                self._health_drops_left -= 1
                return None
            return 200, {
                "status": "ok",
                "loaded": self._reported_loaded(),
                "model": "fake-model",
                "service": "strata",
            }
        if method == "GET" and route == "/status":
            return 200, {"busy": False, "queued": 0, "phase": "idle"}
        if method == "POST" and route == "/unload":
            if self.unload_drop:
                return None
            if self.busy_times > 0:
                self.busy_times -= 1
                return 409, {"status": "busy"}
            if self.unload_status is not None:
                return self.unload_status, {"error": {"message": "nope"}}
            was_loaded = self.loaded
            self._transition(False)
            self._health_drops_left = self.health_drop_after_unload
            return 200, {"status": "unloaded" if was_loaded else "not loaded"}
        if method == "POST" and route == "/load":
            if self.load_fails:
                return 503, {"error": {"message": "gpu busy"}}
            self._transition(True)
            return 200, {"status": "loaded"}
        return super().handle(method, path, body)


class FakeComfy(FakeServer):
    """Mirrors the real ComfyUI endpoints the adapter uses.

    reject: /prompt answers 400 with a node_errors body (node 3 is missing its `images` input).
    history: {prompt_id: entry}; /history/<id> answers {id: entry} once it is there and {} before
        (a prompt that is still pending or running); complete() fills it the way ComfyUI does.
    prompts: the parsed bodies of the /prompt requests that were accepted or rejected.
    queue_posts: the parsed bodies of POST /queue requests (for example {"delete": ["p2"]}).
    Prompt ids are "p1", "p2", ... in submission order.
    """

    def __init__(self, reject: bool = False) -> None:
        super().__init__()
        self.reject = reject
        self.history: dict = {}
        self.prompts: list = []
        self.queue_posts: list = []

    def complete(self, prompt_id: str, files=(), error: str | None = None) -> None:
        """Finish a prompt. files: (filename, subfolder, type) entries of node 9's images output;
        error: the exception message of an execution_error (the prompt then ends in status error)."""
        messages: list = [["execution_start", {"prompt_id": prompt_id}]]
        if error is not None:
            messages.append(
                ["execution_error", {"prompt_id": prompt_id, "node_id": "3", "exception_message": error}]
            )
        outputs = {}
        if files:
            outputs["9"] = {
                "images": [{"filename": n, "subfolder": s, "type": t} for n, s, t in files]
            }
        entry = {
            "prompt": [],
            "outputs": outputs,
            "status": {
                "status_str": "success" if error is None else "error",
                "completed": error is None,
                "messages": messages,
            },
        }
        with self._lock:
            self.history[prompt_id] = entry

    def handle(self, method, path, body):
        route = path.split("?", 1)[0]
        if method == "GET" and route == "/system_stats":
            return 200, {
                "system": {"os": "nt", "comfyui_version": "fake"},
                "devices": [
                    {"name": "cuda:0 Fake GPU", "vram_total": 32 * 2**30, "vram_free": 30 * 2**30}
                ],
            }
        if method == "POST" and route == "/prompt":
            self.prompts.append(json.loads(body.decode("utf-8")))
            if self.reject:
                return 400, {
                    "error": {
                        "type": "prompt_outputs_failed_validation",
                        "message": "Prompt outputs failed validation",
                        "details": "",
                    },
                    "node_errors": {
                        "3": {
                            "errors": [
                                {
                                    "type": "required_input_missing",
                                    "message": "Required input is missing",
                                    "details": "images",
                                    "extra_info": {"input_name": "images"},
                                }
                            ],
                            "dependent_outputs": ["9"],
                            "class_type": "LoadImage",
                        }
                    },
                }
            return 200, {"prompt_id": f"p{len(self.prompts)}", "number": len(self.prompts) - 1, "node_errors": {}}
        if method == "GET" and route.startswith("/history/"):
            prompt_id = route[len("/history/"):]
            return 200, ({prompt_id: self.history[prompt_id]} if prompt_id in self.history else {})
        if method == "POST" and route in ("/free", "/interrupt"):
            return 200, b""  # the real server answers with an empty body
        if method == "GET" and route == "/queue":
            return 200, {"queue_running": [], "queue_pending": []}
        if method == "POST" and route == "/queue":
            self.queue_posts.append(json.loads(body.decode("utf-8")))
            return 200, b""
        return super().handle(method, path, body)


class FakeVoice(FakeServer):
    """Mirrors the one VoiceStudio endpoint the adapter polls: GET /health.

    status: what /health reports (a real backend says "ok" once it can take work).
    """

    def __init__(self, status: str = "ok") -> None:
        super().__init__()
        self.status = status

    def handle(self, method, path, body):
        if method == "GET" and path.split("?", 1)[0] == "/health":
            return 200, {"status": self.status}
        return super().handle(method, path, body)


# --- worker deps -------------------------------------------------------------------------------


class FakeProc:
    """Stands in for the executor's Popen: a pid and a poll() that says it is still running."""

    def __init__(self, pid: int = 4242, code: int | None = None) -> None:
        self.pid = pid
        self.code = code

    def poll(self) -> int | None:
        return self.code


# Not steps, so not recorded: the clock, the VRAM gauge and reading a workflow file.
UNRECORDED = ("now", "sleep", "vram_used", "exec_load_workflow")


def _wait_result(item, prompt_id: str) -> dict:
    """A scripted exec_wait answer: a status string ("done" gets one output file) or a full dict."""
    if isinstance(item, dict):
        return {"seconds": 5.0, "outputs": [], "error": None, **item}
    outputs = [f"out/{prompt_id}.png"] if item == "done" else []
    return {"status": item, "seconds": 5.0, "outputs": outputs, "error": None}


def fake_deps(
    clock: FakeClock | None = None,
    *,
    other_llm=(),
    strata_loaded: bool = True,
    health=None,
    pause: dict | None = None,
    resume: dict | None = None,
    comfy_running: bool = False,
    ready=None,
    port_busy: bool = False,
    starting=(),
    proc_pid: int = 4242,
    proc_code: int | None = None,
    listeners=None,
    children=None,
    variant_error: str | None = None,
    start_error: Exception | None = None,
    cold_start_s: float | None = 12.0,
    load_errors: dict | None = None,
    submit_error: Exception | None = None,
    waits=(),
    wait_advance: float = 0.0,
    on_wait=None,
    commands=(),
    stop: dict | None = None,
):
    """A worker.Deps whose callables record (name, args, kwargs) into deps.calls and answer from a script.

    other_llm: the ports other_llm_alive reports. strata_loaded: the engine's state before the batch;
    without `health`, strata_health reports it and pause/resume change it like the real server.
    health: a list of strata_health answers in call order (the last one repeats).
    pause / resume: the adapters' answers (default: unloaded / loaded after 30 s).
    comfy_running: exec_ready answers True (an executor someone else started).
    ready: a list of exec_ready answers in call order (the last one repeats); wins over comfy_running.
    port_busy: what exec_port_busy answers (something listens on the executor's port).
    starting: the pids exec_starting reports (executor processes found by their command line).
    proc_pid / proc_code: the pid and exit code of the FakeProc exec_start returns (None = running).
    listeners: what exec_listener_pids answers (default: the started FakeProc's pid, none before).
    children: {pid: [descendant pids]} for descendants (default: none), or an exception it raises.
    variant_error: what exec_validate_variant answers (None = the variant is fine).
    start_error: exec_start raises it. cold_start_s: what exec_wait_ready returns (None = never ready).
    load_errors: {workflow basename: exception} for exec_load_workflow. submit_error: every
        exec_submit raises it.
    waits: exec_wait answers in call order (a status string or a dict; default "done"; an exception
        instance is raised instead).
    wait_advance: each exec_wait first advances the clock this far, then asks its `cancelled` probe,
        and answers "cancelled" when the probe says so (like the real wait's poll).
    on_wait(i): called after the i-th exec_wait has decided its answer.
    commands: (returncode, seconds) answers of run_command in call order (default (0, 1.0)).
    stop: exec_stop's answer (default: stopped, the pid of the proc it was given, else 5151).
    The clock (deps.clock) only moves when wait_advance asks it to.
    """
    from gpu_gate import worker  # here, so the adapter tests import this file without the worker

    clock = clock or FakeClock()
    calls: list = []
    engine = {"loaded": strata_loaded}
    counters = {"health": 0, "submit": 0, "wait": 0, "command": 0, "ready": 0}
    spawned: list = []

    def exec_ready():
        if ready is None:
            return comfy_running
        answer = ready[min(counters["ready"], len(ready) - 1)]
        counters["ready"] += 1
        return answer

    def strata_health():
        if health is None:
            return {"status": "ok", "loaded": engine["loaded"]}
        answer = health[min(counters["health"], len(health) - 1)]
        counters["health"] += 1
        return answer

    def strata_pause(log):
        answer = pause or {
            "status": "unloaded" if engine["loaded"] else "not loaded",
            "paused_s": 3.0,
            "tries": 1,
        }
        if answer["status"] in ("unloaded", "not loaded"):
            engine["loaded"] = False
        return answer

    def strata_resume(log):
        answer = resume or {"restored": True, "load_s": 30.0, "error": None, "already_loaded": False}
        if answer.get("restored"):
            engine["loaded"] = True
        return answer

    def exec_start(log_path, log, bat_override=None):
        if start_error is not None:
            raise start_error
        spawned.append(FakeProc(pid=proc_pid, code=proc_code))
        return spawned[-1]

    def exec_listener_pids():
        if listeners is not None:
            return list(listeners)
        return [spawned[-1].pid] if spawned else []

    def descendants(pid):
        if isinstance(children, BaseException):
            raise children
        return list((children or {}).get(pid, []))

    def exec_load_workflow(path):
        error = (load_errors or {}).get(os.path.basename(path))
        if error is not None:
            raise error
        return {"1": {"class_type": "SaveImage", "inputs": {}}}

    def exec_submit(workflow, client_id):
        if submit_error is not None:
            raise submit_error
        counters["submit"] += 1
        return f"p{counters['submit']}"

    def exec_wait(prompt_id, timeout_s, cancelled, alive=None):
        i = counters["wait"]
        counters["wait"] += 1
        if wait_advance:
            clock.sleep(wait_advance)
        item = waits[i] if i < len(waits) else "done"
        if isinstance(item, BaseException):
            raise item
        if cancelled():
            answer = _wait_result("cancelled", prompt_id)
        else:
            answer = _wait_result(item, prompt_id)
        if on_wait is not None:
            on_wait(i)
        return answer

    def run_command(argv, cwd, timeout_s, cancelled, log_path):
        i = counters["command"]
        counters["command"] += 1
        return commands[i] if i < len(commands) else (0, 1.0)

    def exec_stop(proc, log):
        return stop or {"stopped": True, "pids": [proc.pid if proc is not None else 5151]}

    def voice_run_batch(jobs, work_dir, timeout_s, cancelled, log):
        return [
            {"index": i, "status": "done", "seconds": 2.0, "outputs": [job["out"]], "error": None}
            for i, job in enumerate(jobs)
        ]

    script = {
        "strata_health": strata_health,
        "strata_pause": strata_pause,
        "strata_resume": strata_resume,
        "exec_ready": exec_ready,
        "exec_port_busy": lambda: port_busy,
        "exec_starting": lambda: list(starting),
        "exec_listener_pids": exec_listener_pids,
        "descendants": descendants,
        "kill_tree": lambda pid: True,
        "exec_validate_variant": lambda name: variant_error,
        "exec_start": exec_start,
        "exec_wait_ready": lambda proc, timeout_s: cold_start_s,
        "exec_submit": exec_submit,
        "exec_wait": exec_wait,
        "exec_free": lambda: True,
        "exec_stop": exec_stop,
        "exec_load_workflow": exec_load_workflow,
        "exec_delete_pending": lambda prompt_ids: True,
        "voice_run_batch": voice_run_batch,
        "run_command": run_command,
        "vram_used": lambda: 1000,
        "pid_vram_gone": lambda pid, timeout_s: True,
        "other_llm_alive": lambda ports: list(other_llm),
        "now": clock.now,
        "sleep": clock.sleep,
    }

    def recorded(name, fn):
        def call(*args, **kwargs):
            calls.append((name, args, kwargs))
            return fn(*args, **kwargs)

        return call

    deps = worker.Deps(
        **{name: fn if name in UNRECORDED else recorded(name, fn) for name, fn in script.items()}
    )
    deps.calls = calls
    deps.clock = clock
    return deps
