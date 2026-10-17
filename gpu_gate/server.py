"""The MCP stdio server: three tools over newline-delimited JSON-RPC 2.0.

`comfy_batch` / `voice_batch` write a batch file, start the detached worker on it and read the file
every `poll_interval` seconds until the worker writes `finished`; the call blocks for the whole batch.
When the request carries `params._meta.progressToken`, every read of the batch file also sends a
`notifications/progress` (decided jobs / all jobs), which keeps a client's idle timer from ending
the call.
`gate_status` is a read-only snapshot of the state directory and the servers. stdout carries only
protocol lines; diagnostics go to stderr.

Standard library only.
"""

from __future__ import annotations

import json
import sys
import threading
import time
import traceback
from pathlib import Path
from typing import Any, Callable

from . import VERSION, comfyui, config, gpu, procs, state, strata, voicestudio

PROTOCOL_VERSIONS = ("2025-06-18", "2025-03-26", "2024-11-05")
EOF_GRACE_S = 1.0  # at end of input, how long calls in progress get to write their reply
WARN_VRAM_MB = 300  # another process holding this much while the LLM is loaded is worth a warning
WARN_DWM_MB = 3072  # the desktop compositor always holds some; only a lot of it is worth one
MAX_PROGRESS_CHARS = 120  # the message of a progress notification
LAUNCHER = Path(__file__).resolve().parent.parent / "gpu_gate_server.py"

INSTRUCTIONS = (
    "GPU jobs (ComfyUI workflows and commands, VoiceStudio speech) must go through the gpu-gate "
    "tools comfy_batch and voice_batch: submit all of a task's jobs in one call. During the call the "
    "local LLM server is unloaded to free the GPU and reloaded afterwards, so a call can take many "
    "minutes, and your first reply after it is slower while the prompt is read again; that is "
    "normal. Never start ComfyUI or VoiceStudio yourself and never stop or unload the LLM server. "
    "Workflows must be API-format JSON files (ComfyUI \"Export (API)\"): settle the workflow first, "
    "save it, and pass its absolute path. gate_status shows what the gate is doing without changing "
    "anything."
)

_BATCH_SCHEMA = {
    "type": "object",
    "required": ["jobs"],
    "properties": {
        "jobs": {"type": "array", "minItems": 1, "items": {"type": "object"}},
        "options": {"type": "object"},
    },
    "additionalProperties": False,
}

TOOLS = [
    {
        "name": "comfy_batch",
        "description": (
            "Run ComfyUI jobs on the GPU while the local LLM is unloaded; blocks until all are done "
            "and returns per-job status and output file paths. jobs: "
            '{"kind": "workflow", "path": "<absolute path of an API-format workflow JSON>", '
            '"name": "<optional label>"} or {"kind": "command", "command": ["python", "gen.py", ...], '
            '"cwd": "<optional>", "timeout_s": <optional>}. options (all optional): stop_on_error, '
            "job_timeout_s, batch_timeout_s, keep_executor (leave an already running ComfyUI up), "
            "force (go on although another LLM server answers), comfy_variant (another start .bat "
            "in the ComfyUI folder)."
        ),
        "inputSchema": _BATCH_SCHEMA,
    },
    {
        "name": "voice_batch",
        "description": (
            "Run VoiceStudio speech jobs on the GPU while the local LLM is unloaded; blocks until "
            "all are done and returns per-job status and output file paths. jobs: "
            '{"text": "...", "out": "<output .wav path>", "profile_id": ..., "instruct": ..., '
            '"speed": ..., "language": ...} (the batch script\'s job format). options (all '
            "optional): job_timeout_s, batch_timeout_s, keep_executor, force."
        ),
        "inputSchema": _BATCH_SCHEMA,
    },
    {
        "name": "gate_status",
        "description": (
            "Read-only snapshot: the gate's state, the running batch's progress, whether the LLM is "
            "loaded, whether ComfyUI/VoiceStudio are up, GPU memory per process, warnings and the "
            "last batch result."
        ),
        "inputSchema": {"type": "object", "properties": {}, "additionalProperties": False},
        "annotations": {"readOnlyHint": True},
    },
]

_TOOL_NAMES = [tool["name"] for tool in TOOLS]
_BATCH_KINDS = {"comfy_batch": "comfyui", "voice_batch": "voicestudio"}


# --- small helpers -----------------------------------------------------------------------------


def _one_line(text: object) -> str:
    return " ".join(str(text).split())


def _describe(err: BaseException) -> str:
    return _one_line(f"{type(err).__name__}: {err}")


def _file_log(path: Path) -> Callable[[str], None]:
    def log(message: str) -> None:
        try:
            with open(path, "a", encoding="utf-8") as f:
                f.write(f"{state.now_iso()} server: {message}\n")
        except OSError:
            pass

    return log


_TYPES = {
    "object": lambda v: isinstance(v, dict),
    "array": lambda v: isinstance(v, list),
    "string": lambda v: isinstance(v, str),
    "integer": lambda v: isinstance(v, int) and not isinstance(v, bool),
    "boolean": lambda v: isinstance(v, bool),
}
_ARTICLE = {"object": "an object", "array": "an array", "integer": "an integer"}


def check_schema(schema: dict, value: Any, where: str = "") -> str | None:
    """Why value does not fit schema ("jobs: expected a non-empty array"), or None.

    Supports type, required, minItems, items, properties and additionalProperties: false.
    """
    prefix = f"{where}: " if where else ""
    kind = schema.get("type")
    if kind and not _TYPES[kind](value):
        return f"{prefix}expected {_ARTICLE.get(kind, 'a ' + kind)}"
    if kind == "array":
        least = schema.get("minItems", 0)
        if len(value) < least:
            wanted = "a non-empty array" if least == 1 else f"an array of at least {least} items"
            return f"{prefix}expected {wanted}"
        items = schema.get("items")
        if items:
            for i, item in enumerate(value):
                problem = check_schema(items, item, f"{where}[{i}]")
                if problem:
                    return problem
    if kind == "object":
        props = schema.get("properties", {})
        for key in schema.get("required", []):
            if key not in value:
                return f"{key}: required"
        if schema.get("additionalProperties") is False:
            for key in value:
                if key not in props:
                    return f"{key}: unknown argument"
        for key, sub in props.items():
            if key in value:
                problem = check_schema(sub, value[key], f"{where}.{key}" if where else key)
                if problem:
                    return problem
    return None


def worker_argv(batch_path: str) -> list[str]:
    """The command that runs the worker on batch_path: the root launcher when it is there."""
    target = [str(LAUNCHER)] if LAUNCHER.is_file() else ["-m", "gpu_gate"]
    return [sys.executable, *target, "--worker", str(batch_path)]


def default_spawn_worker(home: Path) -> Callable[[str], int]:
    """spawn_worker for real use: a detached process logging to logs/<batch_id>.log."""
    children: list = []  # kept so a running Popen is never garbage-collected (ResourceWarning)

    def spawn(batch_path: str) -> int:
        children[:] = [p for p in children if p.poll() is None]
        log_path = state.log_path(home, Path(batch_path).stem)
        proc = procs.spawn_detached(worker_argv(batch_path), None, str(log_path))
        children.append(proc)
        return proc.pid

    return spawn


class _ToolError(Exception):
    """A tool error; res is the structured result ({"error": "<one line>", ...})."""

    def __init__(self, res: dict):
        super().__init__(res.get("error"))
        self.res = res


class _RpcError(Exception):
    def __init__(self, code: int, message: str):
        super().__init__(message)
        self.code = code
        self.message = message


def _error(rid: Any, code: int, message: str) -> dict:
    return {"jsonrpc": "2.0", "id": rid, "error": {"code": code, "message": message}}


def _valid_id(value: Any) -> bool:
    """JSON-RPC ids this server accepts: a string, an integer (not a bool) or null."""
    return value is None or isinstance(value, str) or (isinstance(value, int) and not isinstance(value, bool))


def _progress_token(params: dict) -> str | int | None:
    """params._meta.progressToken when it is a string or an integer (not a bool), else None."""
    meta = params.get("_meta")
    token = meta.get("progressToken") if isinstance(meta, dict) else None
    if isinstance(token, str) or (isinstance(token, int) and not isinstance(token, bool)):
        return token
    return None


def _count(value: Any) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) and value >= 0 else None


def progress_params(token: str | int, doc: dict) -> dict:
    """The params of a notifications/progress for the batch file doc: progress is the number of
    decided jobs, total the number of jobs (left out when unknown), message
    "<state> <done>/<total> <current job>" in at most MAX_PROGRESS_CHARS characters."""
    progress = doc.get("progress") if isinstance(doc.get("progress"), dict) else {}
    done = _count(progress.get("done")) or 0
    total = _count(progress.get("total"))
    current = progress.get("current") or ""
    message = _one_line(f"{doc.get('state') or 'pending'} {done}/{'?' if total is None else total} {current}")
    params: dict = {"progressToken": token, "progress": done}
    if total is not None:
        params["total"] = total
    params["message"] = message[:MAX_PROGRESS_CHARS]
    return params


def _tool_result(res: dict, is_error: bool) -> dict:
    return {
        "content": [{"type": "text", "text": json.dumps(res, ensure_ascii=False, indent=1)}],
        "structuredContent": res,
        "isError": is_error,
    }


# --- the server --------------------------------------------------------------------------------


class Server:
    def __init__(
        self,
        home: Path | str,
        cfg_loader: Callable[[], config.Config],
        spawn_worker: Callable[[str], int],
        poll_interval: float = 2.0,
        sleep: Callable[[float], None] = time.sleep,
        process_vram: Callable[[], list | None] = gpu.process_vram,
    ):
        self.home = Path(home)
        self.cfg_loader = cfg_loader
        self.spawn_worker = spawn_worker
        self.poll_interval = poll_interval
        self.sleep = sleep
        self.process_vram = process_vram
        self._mu = threading.Lock()  # guards _running
        # request id of a tools/call in progress -> {"batch_id", "pid", "cancelled"}
        self._running: dict = {}
        self._batch_mu = threading.Lock()  # one batch call per process
        self._write_mu = threading.Lock()
        self._out = None

    # -- JSON-RPC --

    def handle(self, msg: Any) -> dict | None:
        """One JSON-RPC message in, its reply out (None for notifications and responses)."""
        if not isinstance(msg, dict):
            return _error(None, -32600, "invalid request")
        method = msg.get("method")
        if "id" not in msg:
            if method == "notifications/cancelled":
                params = msg.get("params")
                self._on_cancelled(params.get("requestId") if isinstance(params, dict) else None)
            return None
        rid = msg["id"]
        if not _valid_id(rid):
            return _error(None, -32600, "invalid request: id must be a string or an integer")
        if not isinstance(method, str):
            return None if ("result" in msg or "error" in msg) else _error(rid, -32600, "invalid request")
        params = msg.get("params")
        if params is None:
            params = {}
        if not isinstance(params, dict):
            return _error(rid, -32602, "params must be an object")
        methods = {
            "initialize": self._initialize,
            "ping": lambda p, r: {},
            "tools/list": lambda p, r: {"tools": TOOLS},
            "tools/call": self._tools_call,
            "resources/list": lambda p, r: {"resources": []},
            "prompts/list": lambda p, r: {"prompts": []},
        }
        fn = methods.get(method)
        if fn is None:
            return _error(rid, -32601, f"method not found: {method}")
        try:
            result = fn(params, rid)
        except _RpcError as err:
            return _error(rid, err.code, err.message)
        except Exception as err:
            traceback.print_exc(file=sys.stderr)
            return _error(rid, -32603, f"internal error: {_describe(err)}")
        return {"jsonrpc": "2.0", "id": rid, "result": result}

    def _initialize(self, params: dict, rid: Any) -> dict:
        asked = params.get("protocolVersion")
        return {
            "protocolVersion": asked if asked in PROTOCOL_VERSIONS else PROTOCOL_VERSIONS[0],
            "capabilities": {"tools": {"listChanged": False}},
            "serverInfo": {"name": "gpu-gate", "version": VERSION},
            "instructions": INSTRUCTIONS,
        }

    def _record(self, rid: Any) -> dict:
        with self._mu:
            return self._running.setdefault(rid, {"batch_id": None, "pid": None, "cancelled": False})

    def _tools_call(self, params: dict, rid: Any) -> dict:
        self._record(rid)
        try:
            return self._call_tool(params, rid)
        finally:
            with self._mu:
                self._running.pop(rid, None)

    def _call_tool(self, params: dict, rid: Any) -> dict:
        name = params.get("name")
        if name not in _TOOL_NAMES:
            raise _RpcError(-32602, f"unknown tool: {name}")
        args = params.get("arguments")
        if args is None:
            args = {}
        schema = next(t["inputSchema"] for t in TOOLS if t["name"] == name)
        problem = "arguments: expected an object" if not isinstance(args, dict) else check_schema(schema, args)
        if problem:
            return _tool_result({"error": problem}, True)
        try:
            if name == "gate_status":
                return _tool_result(self.gate_status(), False)
            return _tool_result(self._batch(_BATCH_KINDS[name], args, rid, _progress_token(params)), False)
        except _ToolError as err:
            return _tool_result(err.res, True)
        except Exception as err:
            traceback.print_exc(file=sys.stderr)
            return _tool_result({"error": f"internal error in {name}: {_describe(err)}"}, True)

    # -- cancel --

    def _on_cancelled(self, rid: Any) -> None:
        """Cancel the batch this process runs for request rid; other ids are ignored."""
        if rid is None or not _valid_id(rid):
            return
        with self._mu:
            record = self._running.get(rid)
            if record is None:
                return
            record["cancelled"] = True
            batch_id = record["batch_id"]
        if batch_id is not None:
            state.request_cancel(self.home, batch_id)

    def _register_batch(self, rid: Any, batch_id: str) -> None:
        with self._mu:
            record = self._running.get(rid)
            if record is None:
                return
            record["batch_id"] = batch_id
            cancelled = record["cancelled"]
        if cancelled:  # the client cancelled before the batch had an id
            state.request_cancel(self.home, batch_id)

    # -- batches --

    def _live_batch(self) -> dict | None:
        """The running batch: its lock plus kind, state and progress from its batch file."""
        lock = state.current_lock(self.home)
        if lock is None:
            return None
        doc = state.read_json(state.batch_path(self.home, str(lock.get("batch_id")))) or {}
        return {
            "batch_id": lock.get("batch_id"),
            "pid": lock.get("pid"),
            "started": lock.get("started"),
            "kind": doc.get("kind"),
            "state": doc.get("state"),
            "progress": doc.get("progress"),
        }

    def _resume_if_unloaded(self, cfg: config.Config, log: Callable[[str], None]) -> dict | None:
        """Load Strata again when it answers and says its engine is not loaded."""
        health = strata.health(cfg.strata)
        if health is None or health.get("loaded") is not False:
            return None
        log("strata is not loaded after an aborted batch; loading it")
        return strata.resume(cfg.strata, log)

    def _release_if_ours(self, batch_id: str) -> None:
        lock = state.read_json(state.paths(self.home).lock)
        if lock is not None and lock.get("batch_id") == batch_id:
            state.release_lock(self.home)

    def _release_quietly(self, batch_id: str) -> None:
        """_release_if_ours for cleanup paths: a failure is reported on stderr, never raised."""
        try:
            self._release_if_ours(batch_id)
        except Exception as err:
            print(f"gpu-gate: could not release the lock of batch {batch_id}: {_describe(err)}", file=sys.stderr)

    def _batch(self, kind: str, args: dict, rid: Any, token: str | int | None = None) -> dict:
        if not self._batch_mu.acquire(blocking=False):
            raise _ToolError({"error": "busy", "batch": self._live_batch()})
        try:
            return self._run_batch(kind, args, rid, token)
        finally:
            self._batch_mu.release()

    def _progress_sender(self, rid: Any, token: str | int | None) -> Callable[[dict], None] | None:
        """notify(doc) sending a notifications/progress for request rid, or None without a token.
        Some clients (Qwen Code) end a call after a stretch without messages; progress resets
        that timer. Nothing is sent once the request is cancelled."""
        if token is None:
            return None

        def notify(doc: dict) -> None:
            with self._mu:
                record = self._running.get(rid)
                if record is None or record["cancelled"]:
                    return
            self._send({"jsonrpc": "2.0", "method": "notifications/progress",
                        "params": progress_params(token, doc)})

        return notify

    def _run_batch(self, kind: str, args: dict, rid: Any, token: str | int | None = None) -> dict:
        try:
            cfg = self.cfg_loader()
        except config.ConfigError as err:
            raise _ToolError({"error": _one_line(err)}) from None
        batch_id = state.new_batch_id()
        lock = state.acquire_lock(self.home, batch_id)
        if lock is None:
            raise _ToolError({"error": "busy", "batch": self._live_batch()})
        try:
            path, pid, log_path, log, doc = self._start(cfg, kind, args, rid, lock, batch_id)
        except BaseException:
            # The lock holds this server's pid: left behind, every later batch would be busy.
            self._release_quietly(batch_id)
            raise
        state.write_lock(self.home, batch_id, pid)
        with self._mu:
            record = self._running.get(rid)
            if record is not None:
                record["pid"] = pid
        return self._wait(cfg, path, doc, batch_id, pid, log_path, log, self._progress_sender(rid, token))

    def _start(self, cfg, kind: str, args: dict, rid: Any, lock: state.Lock, batch_id: str):
        """Everything between taking the lock and the worker's pid: the batch file and the spawn."""
        self._register_batch(rid, batch_id)
        path = state.batch_path(self.home, batch_id)
        log_path = state.log_path(self.home, batch_id)
        log = _file_log(log_path)
        jobs = args["jobs"]
        warnings: list[str] = []
        doc = {
            "batch_id": batch_id, "kind": kind, "jobs": jobs, "options": args.get("options") or {},
            "state": "pending", "progress": {"done": 0, "total": len(jobs), "current": None},
            "warnings": warnings, "result": None, "started": state.now_iso(), "finished": None,
            "pid": None,
        }
        if lock.stale_batch_id:
            warnings.append(f"previous batch {lock.stale_batch_id} was aborted")
            log(warnings[-1])
            try:
                restored = self._resume_if_unloaded(cfg, log)
            except Exception as err:
                restored = {"restored": False, "error": _describe(err)}
            if restored is not None and not restored.get("restored"):
                warnings.append(f"reloading the LLM after the aborted batch failed: {restored.get('error')}")
        try:
            state.write_json(path, doc)
            pid = self.spawn_worker(str(path))
        except Exception as err:
            self._abort(path, doc, batch_id)
            raise _ToolError({"error": f"could not start the worker: {_describe(err)}"}) from err
        return path, pid, log_path, log, doc

    def _abort(self, path: Path, doc: dict, batch_id: str) -> None:
        """Mark the batch aborted (best effort: often the disk is why we got here) and release
        the lock whatever happens."""
        try:
            try:
                current = state.read_json(path) or doc
                current.update(state="aborted", finished=state.now_iso())
                state.write_json(path, current)
            except Exception:
                pass
            try:
                state.set_state(self.home, "aborted", batch_id)
            except Exception:
                pass
        finally:
            self._release_quietly(batch_id)

    def _wait(self, cfg, path: Path, doc: dict, batch_id: str, pid: int, log_path: Path, log,
              notify: Callable[[dict], None] | None = None) -> dict:
        while True:
            current = state.read_json(path)
            if notify is not None and current is not None:
                notify(current)
            if current is None or not current.get("finished"):
                if procs.pid_alive(pid):
                    self.sleep(self.poll_interval)
                    continue
                current = state.read_json(path)  # it may have finished just before it exited
            if current is not None and current.get("finished"):
                self._release_if_ours(batch_id)
                result = current.get("result")
                if not isinstance(result, dict):
                    raise _ToolError({"error": f"batch {batch_id} ended without a result "
                                               f"(state {current.get('state')}); see {log_path}"})
                return result
            log(f"worker pid {pid} is gone and the batch is not finished")
            self._abort(path, doc, batch_id)
            try:
                self._resume_if_unloaded(cfg, log)
            except Exception as err:
                log(f"reloading the LLM failed: {_describe(err)}")
            raise _ToolError({"error": f"worker died; see {log_path}"})

    # -- gate_status --

    def gate_status(self) -> dict:
        """The snapshot; never raises: a probe that fails becomes a warning."""
        warnings: list[str] = []

        def probe(what: str, fn: Callable[[], Any], default: Any = None) -> Any:
            try:
                return fn()
            except Exception as err:
                warnings.append(f"{what} failed: {_describe(err)}")
                return default

        st = probe("reading state.json", lambda: state.read_state(self.home), {}) or {}
        now_state = st.get("state", "idle")
        raw_lock = probe("reading the lock", lambda: state.read_json(state.paths(self.home).lock))
        lock_pid = raw_lock.get("pid") if isinstance(raw_lock, dict) else None
        # Judged on the lock as read, so a batch that ends between the two reads is not "aborted".
        dead_lock = isinstance(raw_lock, dict) and not (
            isinstance(lock_pid, int) and not isinstance(lock_pid, bool)
            and probe("checking the worker pid", lambda: procs.pid_alive(lock_pid), True)
        )
        batch = probe("reading the batch", self._live_batch)
        if batch is not None:
            now_state = batch.get("state") or now_state
        elif dead_lock:
            now_state = "aborted"
            warnings.append(
                f"batch {raw_lock.get('batch_id')} was aborted: its worker (pid {raw_lock.get('pid')}) "
                "is not running"
            )

        strata_view = {"server": None, "loaded": None, "busy": None}
        executor = {"comfyui": None, "voicestudio": None}
        cfg = None
        try:
            cfg = self.cfg_loader()
        except Exception as err:
            warnings.append(_one_line(err))
        if cfg is not None:
            health = probe("strata health", lambda: strata.health(cfg.strata))
            strata_view["server"] = health is not None
            if health is not None:
                strata_view["loaded"] = health.get("loaded")
                busy = probe("strata status", lambda: strata.status(cfg.strata))
                strata_view["busy"] = busy.get("busy") if isinstance(busy, dict) else None
            for kind, ready in (("comfyui", lambda: comfyui.ready(cfg.comfyui)),
                                ("voicestudio", lambda: voicestudio.ready(cfg.voicestudio))):
                if not probe(f"{kind} probe", ready, False):
                    executor[kind] = "down"
                elif batch is not None and batch.get("kind") == kind:
                    executor[kind] = "up(gate)"
                else:
                    executor[kind] = "up(external)"

        listing = probe("GPU process memory", self.process_vram)
        if listing is None:
            warnings.append("GPU process memory unavailable")
        elif strata_view["loaded"]:
            for entry in listing:
                name = str(entry.get("name") or "unknown")
                mb = entry.get("vram_mb") or 0
                if "strata" in name.lower():
                    continue  # the LLM itself
                too_much = mb > WARN_DWM_MB if name.lower() == "dwm" else mb >= WARN_VRAM_MB
                if too_much:
                    warnings.append(f"{name}(pid {entry.get('pid')}) holds {mb} MB while the LLM is loaded")

        return {
            "state": now_state,
            "batch": batch,
            "strata": strata_view,
            "executor": executor,
            "gpu": listing,
            "warnings": warnings,
            "last_result": st.get("last_result"),
        }

    # -- stdio --

    def _send(self, reply: dict) -> None:
        data = json.dumps(reply, ensure_ascii=False).encode("utf-8") + b"\n"
        with self._write_mu:
            if self._out is None:
                return  # handle() called directly (tests, --selftest): there is no stream
            try:
                self._out.write(data)
                self._out.flush()
            except (OSError, ValueError):
                pass  # the client has gone; the detached worker goes on regardless

    def _call_thread(self, msg: dict, record: dict) -> None:
        reply = self.handle(msg)
        if reply is not None and not record["cancelled"]:  # no reply to a cancelled request (MCP)
            self._send(reply)

    def _serve_line(self, raw: bytes):
        """Answer one input line; a tools/call is started on a thread, returned as (thread, record)."""
        line = raw.strip()
        if not line:
            return None
        try:
            msg = json.loads(line.decode("utf-8"))
        except (UnicodeDecodeError, ValueError):
            self._send(_error(None, -32700, "parse error"))
            return None
        if (isinstance(msg, dict) and msg.get("method") == "tools/call" and "id" in msg
                and _valid_id(msg["id"])):
            record = self._record(msg["id"])  # now, so a cancel that comes right after counts
            thread = threading.Thread(target=self._call_thread, args=(msg, record), daemon=True)
            thread.start()
            return thread, record
        reply = self.handle(msg)
        if reply is not None:
            self._send(reply)
        return None

    def serve(self, inp, out) -> None:
        """Read one JSON-RPC message per line from inp (bytes) and write replies to out (bytes).

        tools/call runs on a (daemon) thread so ping and notifications/cancelled are answered during
        a batch. At end of input the calls in progress get EOF_GRACE_S to answer; a batch still
        running then is left to its detached worker (its result lands in the batch file and
        state.json), so a client waiting for this process to exit is not held up by it.
        """
        self._out = out
        threads: list[tuple[threading.Thread, dict]] = []
        for raw in inp:
            try:
                started = self._serve_line(raw)
            except Exception:
                traceback.print_exc(file=sys.stderr)  # one bad line must not end the loop
                continue
            if started is not None:
                threads = [(t, r) for t, r in threads if t.is_alive()] + [started]
        deadline = time.monotonic() + EOF_GRACE_S
        for thread, record in threads:
            thread.join(max(0.0, deadline - time.monotonic()))
            if thread.is_alive() and record["batch_id"] is not None:
                print(
                    f"gpu-gate: stdin closed; batch {record['batch_id']} continues in worker pid "
                    f"{record['pid']}",
                    file=sys.stderr,
                )


def make_server(home: Path) -> Server:
    """The server with the real config loader and worker spawner."""
    return Server(home, lambda: config.load(home), default_spawn_worker(home))
