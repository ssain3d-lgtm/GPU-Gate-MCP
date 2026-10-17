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
        status, payload = self.server.owner._record_and_handle(
            method, self.path, Headers(self.headers.items()), body
        )
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
        """Return (status, payload); called with the lock held. A bytes payload is sent as is."""
        return 404, {"error": f"no route: {method} {path}"}


class FakeStrata(FakeServer):
    """Mirrors the real Strata endpoints the adapter uses.

    busy_times: the first N /unload calls answer 409 {"status": "busy"}.
    loaded: whether the engine starts loaded.
    load_fails: /load answers 503 instead of loading.
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
    ) -> None:
        super().__init__()
        self.busy_times = busy_times
        self.absent = absent
        self.loaded = loaded
        self.load_fails = load_fails
        self.health_lag = health_lag
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
            return 200, {
                "status": "ok",
                "loaded": self._reported_loaded(),
                "model": "fake-model",
                "service": "strata",
            }
        if method == "GET" and route == "/status":
            return 200, {"busy": False, "queued": 0, "phase": "idle"}
        if method == "POST" and route == "/unload":
            if self.busy_times > 0:
                self.busy_times -= 1
                return 409, {"status": "busy"}
            was_loaded = self.loaded
            self._transition(False)
            return 200, {"status": "unloaded" if was_loaded else "not loaded"}
        if method == "POST" and route == "/load":
            if self.load_fails:
                return 503, {"error": {"message": "gpu busy"}}
            self._transition(True)
            return 200, {"status": "loaded"}
        return super().handle(method, path, body)
