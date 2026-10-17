"""Fake servers and a fake clock for the adapter tests.

Each fake is a real HTTP server on 127.0.0.1 with an ephemeral port, so the adapters are tested over
real sockets. They share FakeServer (start/stop, call recording, JSON replies); a subclass only
implements handle(method, path, body) -> (status, payload).
"""

from __future__ import annotations

import json
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
    Prompt ids are "p1", "p2", ... in submission order.
    """

    def __init__(self, reject: bool = False) -> None:
        super().__init__()
        self.reject = reject
        self.history: dict = {}
        self.prompts: list = []

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
        return super().handle(method, path, body)
