"""ComfyUI adapter: start it the way the user does, run API-format workflows, collect the outputs.

ComfyUI is launched by parsing the user's own "Start ComfyUI.bat" (its `set` lines and its python
line), so the batch sees exactly the environment and arguments a double-click would give it, minus
the browser window. Workflows are submitted over ComfyUI's HTTP API and polled until they finish.

Standard library only. `sleep` and `now` are injectable so tests never wait in real time; `log` is a
callable taking one line of text. The bat may hold secrets (an API key in a `set` line), so only the
names of the environment variables it sets are ever logged, never their values.
"""

from __future__ import annotations

import json
import locale
import os
import re
import subprocess
import time
from pathlib import Path
from typing import Callable

from . import http, procs
from .config import ComfyCfg

READY_POLL_S = 1.0  # how often wait_ready asks /system_stats
WAIT_POLL_S = 1.5  # how often wait asks /history
SUBMIT_TIMEOUT_S = 30  # ComfyUI validates the whole workflow before it answers /prompt
FREE_TIMEOUT_S = 30
OUTPUT_KEYS = ("images", "gifs", "videos", "audio")  # the keys of a history output that hold files
MAX_ERROR_CHARS = 300  # one node's validation message (a "value not in list" error names every model)
MIN_REDACT_CHARS = 4  # bat values shorter than this are not hidden in the start log line

Log = Callable[[str], None]


class SubmitError(Exception):
    """ComfyUI refused the workflow; str() says which nodes and why."""


# --- parsing the user's bat --------------------------------------------------------------------

_SKIPPED = re.compile(r"@|::|(?:title|echo|pause|exit|rem)\b", re.IGNORECASE)
_SET = re.compile(r"set\s+([^/\s].*)", re.IGNORECASE)  # not `set /a` or `set /p`
_TOKEN = re.compile(r'(?:"[^"]*"|[^\s"])+')  # a run of non-space characters, quoted parts included
_DP0 = re.compile(r"%~dp0", re.IGNORECASE)
_VAR = re.compile(r"%([^%\s=]+)%")


def _read_bat(path: str) -> str:
    """The bat's text: UTF-8 if it is, else the console's code page (a Korean Windows writes cp949)."""
    raw = Path(path).read_bytes()
    try:
        return raw.decode("utf-8-sig")
    except UnicodeDecodeError:
        return raw.decode(locale.getpreferredencoding(False), errors="replace")


def _lookup(name: str, env: dict[str, str]) -> str:
    """%NAME%: the variables the bat has set so far, else the real environment, else empty."""
    for key, value in env.items():
        if key.upper() == name.upper():  # Windows variable names ignore case
            return value
    return os.environ.get(name, "")


def _expand(text: str, env: dict[str, str], dp0: str) -> str:
    text = _DP0.sub(lambda _: dp0, text)  # first: its value ends in a path separator, not a `%`
    return _VAR.sub(lambda m: _lookup(m.group(1), env), text)


def _collect_set(rest: str, env: dict[str, str], dp0: str) -> None:
    """Record `NAME=value` (the text after `set`, with or without the surrounding quotes)."""
    if rest.startswith('"'):  # cmd ignores everything after the closing quote
        end = rest.rfind('"')
        rest = rest[1:end] if end > 0 else rest[1:]
    name, equals, value = rest.partition("=")
    name = name.strip()
    if not equals or not name:
        return
    value = _expand(value, env, dp0)  # before the old value goes away: `set "PATH=x;%PATH%"`
    for key in [k for k in env if k.upper() == name.upper()]:
        del env[key]
    env[name] = value


def _anchored(token: str, base: str, first: bool) -> str:
    """Make a relative path token absolute against the bat's folder; any other token stays as it is.

    Only the program (first token, when it is a path rather than a bare name looked up on PATH) and
    the .py scripts are paths. Backslashes become the platform separator.
    """
    fixed = token.replace("\\", os.sep).replace("/", os.sep)
    if os.path.splitdrive(fixed)[0] or fixed.startswith(os.sep):  # already absolute
        return token
    if (first and os.sep in fixed) or fixed.lower().endswith(".py"):
        return os.path.normpath(os.path.join(base, fixed))
    return token


def parse_bat(path: str) -> tuple[list[str], dict[str, str]]:
    """The command line and environment a ComfyUI bat sets up: (argv, env).

    env holds only what the bat sets with `set NAME=value` / `set "NAME=value"` before its python
    line (the caller merges it over the real environment). `%~dp0` becomes the bat's folder with a
    trailing separator; `%VAR%` comes from the bat's own variables, then the real environment, else
    empty. The python line is the first line with a token ending in `main.py`; its relative paths are
    made absolute against the bat's folder and `--disable-auto-launch` is added when it is absent.
    """
    bat_dir = os.path.dirname(os.path.abspath(path))
    dp0 = os.path.join(bat_dir, "")
    env: dict[str, str] = {}
    for raw in _read_bat(path).splitlines():
        line = raw.strip()
        if not line or _SKIPPED.match(line):
            continue
        match = _SET.fullmatch(line)
        if match:
            _collect_set(match.group(1), env, dp0)
            continue
        tokens = [t.replace('"', "") for t in _TOKEN.findall(_expand(line, env, dp0))]
        if not any(t.lower().endswith("main.py") for t in tokens):
            continue
        argv = [_anchored(t, bat_dir, i == 0) for i, t in enumerate(tokens)]
        if "--disable-auto-launch" not in argv:
            argv.append("--disable-auto-launch")
        return argv, env
    raise ValueError(f"no python line with main.py in {path}")


def variant_error(cfg: ComfyCfg, name: object) -> str | None:
    """Why command() would refuse the variant name ("comfy_variant not found: <name>"), or None.

    Checked before anything is touched, so a bad name does not cost an LLM unload. A configured
    cfg.command wins over every bat, so then any name passes (command() ignores it).
    """
    if cfg.command:
        return None
    bare = (
        isinstance(name, str) and bool(name)
        and os.path.basename(name) == name and "\\" not in name
    )
    if not bare or not os.path.isfile(os.path.join(cfg.dir, name)):
        return f"comfy_variant not found: {name}"
    return None


def _bat_path(cfg: ComfyCfg, bat_override: str | None) -> str:
    """cfg.bat in cfg.dir, or the bare file name bat_override naming another bat in that folder."""
    if not bat_override:
        return os.path.join(cfg.dir, cfg.bat)
    problem = variant_error(cfg, bat_override)
    if problem:
        raise ValueError(problem)
    return os.path.join(cfg.dir, bat_override)


def command(cfg: ComfyCfg, bat_override: str | None = None) -> tuple[list[str], dict, str | None]:
    """(argv, env, cwd) to start ComfyUI.

    cfg.command, when set, is used as is (run in cfg.dir) and wins over any bat, bat_override
    included. Otherwise the bat is parsed: cfg.bat, or bat_override, a bare file name that must exist
    in cfg.dir (else ValueError "comfy_variant not found: <name>"). The bat's folder is the cwd, as
    the bat's own `cd /D %~dp0` makes it.
    """
    if cfg.command:
        return list(cfg.command), {}, cfg.dir
    bat = _bat_path(cfg, bat_override)
    argv, env = parse_bat(bat)
    return argv, env, os.path.dirname(os.path.abspath(bat))


def env_keys(env: dict) -> list[str]:
    """The variable names only: what is safe to log about an environment."""
    return sorted(env)


def _redact_argv(argv: list[str], env: dict[str, str]) -> list[str]:
    """argv for a log line: each value the bat set (`%LM_API%` on the python line expands into argv)
    is replaced by `<NAME>`. Values shorter than MIN_REDACT_CHARS are left alone, so a flag like
    `set N=1` does not mangle every `1` in the line. Longer values go first, in case one contains
    another."""
    secrets = sorted(
        ((name, value) for name, value in env.items() if len(value) >= MIN_REDACT_CHARS),
        key=lambda item: len(item[1]),
        reverse=True,
    )
    shown = []
    for arg in argv:
        for name, value in secrets:
            arg = arg.replace(value, f"<{name}>")
        shown.append(arg)
    return shown


def _spawn_env(env: dict[str, str]) -> dict[str, str]:
    """The real environment with the bat's variables on top (names compare without case on Windows)."""
    merged = dict(os.environ)
    if procs.IS_WINDOWS:
        wanted = {key.upper() for key in env}
        merged = {k: v for k, v in merged.items() if k.upper() not in wanted}
    merged.update(env)
    return merged


# --- starting ----------------------------------------------------------------------------------


def ready(cfg: ComfyCfg) -> bool:
    """True when ComfyUI answers GET /system_stats."""
    code, _ = http.http_json(cfg.url.rstrip("/") + "/system_stats")
    return code == 200


def start(
    cfg: ComfyCfg, log_path: str, log: Log, bat_override: str | None = None
) -> subprocess.Popen:
    """Start ComfyUI detached, its output appended to log_path. The log gets the argv and the names
    of the env variables set, never their values: a value that reached the argv through `%NAME%`
    shows as `<NAME>` (the process itself still gets the real argv)."""
    argv, env, cwd = command(cfg, bat_override)
    keys = ", ".join(env_keys(env)) or "none"
    log(f"comfyui: starting {subprocess.list2cmdline(_redact_argv(argv, env))} (env keys: {keys})")
    return procs.spawn_detached(argv, cwd, log_path, env=_spawn_env(env))


def wait_ready(
    cfg: ComfyCfg,
    proc: subprocess.Popen,
    timeout_s: float,
    sleep: Callable[[float], None] = time.sleep,
    now: Callable[[], float] = time.monotonic,
) -> float | None:
    """Poll until ComfyUI answers; the seconds it took, or None when the process exits first or
    timeout_s passes (the caller tells them apart with proc.poll())."""
    t0 = now()
    while True:
        if ready(cfg):
            return now() - t0
        if proc.poll() is not None or now() - t0 >= timeout_s:
            return None
        sleep(READY_POLL_S)


# --- running a workflow ------------------------------------------------------------------------


def load_workflow(path: str) -> dict:
    """An API-format workflow from a JSON file; the UI-format file that "Save" writes is refused."""
    with open(path, encoding="utf-8-sig") as f:
        try:
            data = json.load(f)
        except ValueError as err:
            raise ValueError(f"not valid JSON: {path} ({err})") from err
    if not isinstance(data, dict) or (
        "nodes" in data and "links" in data and not any(key.isdigit() for key in data)
    ):
        raise ValueError(f"not an API-format workflow (save with 'Export (API)'): {path}")
    return data


def _node_text(error: object) -> str:
    """'<message>: <details>' of one validation error, cut to a sensible length."""
    if isinstance(error, str):
        message, details = error, ""
    elif isinstance(error, dict):
        message = error.get("message") or error.get("type") or "invalid"
        details = error.get("details") or ""
    else:
        return ""
    text = " ".join((f"{message}: {details}" if details else str(message)).split())
    return text if len(text) <= MAX_ERROR_CHARS else text[: MAX_ERROR_CHARS - 3] + "..."


def _summary(body: object) -> str:
    """'node 3: Required input is missing: images; node 7: ...' from a rejected /prompt answer.

    One entry per node in node_errors (its first error); without any, the top-level error message.
    "" when the body says nothing usable.
    """
    if not isinstance(body, dict):
        return ""
    parts = []
    node_errors = body.get("node_errors")
    if isinstance(node_errors, dict):
        for node_id, info in node_errors.items():
            errors = info.get("errors") if isinstance(info, dict) else None
            first = errors[0] if isinstance(errors, list) and errors else None
            text = _node_text(first)
            if text:
                parts.append(f"node {node_id}: {text}")
    return "; ".join(parts) or _node_text(body.get("error"))


def submit(cfg: ComfyCfg, workflow: dict, client_id: str) -> str:
    """Queue an API-format workflow; the prompt id. Raises SubmitError when ComfyUI refuses it."""
    code, body = http.http_json(
        cfg.url.rstrip("/") + "/prompt",
        "POST",
        {"prompt": workflow, "client_id": client_id},
        timeout=SUBMIT_TIMEOUT_S,
    )
    if code == 200 and isinstance(body, dict) and body.get("prompt_id"):
        return str(body["prompt_id"])
    if code == 0:
        raise SubmitError(f"no answer from ComfyUI at {cfg.url}")
    if code == 400:
        raise SubmitError(_summary(body) or "ComfyUI rejected the workflow (HTTP 400)")
    raise SubmitError(f"unexpected answer from ComfyUI: HTTP {code}")


def _output_dir(cfg: ComfyCfg) -> str:
    """Where ComfyUI writes its output: cfg.output_dir, else <cfg.dir>/ComfyUI/output."""
    if cfg.output_dir:
        return cfg.output_dir
    return os.path.join(cfg.dir, "ComfyUI", "output") if cfg.dir else ""


def _output_paths(cfg: ComfyCfg, entry: dict) -> list[str]:
    """The files a finished prompt saved (type "output" only; temp and input files are skipped)."""
    base = _output_dir(cfg)  # "" when neither output_dir nor dir is known: paths stay relative
    paths: list[str] = []
    outputs = entry.get("outputs")
    if not isinstance(outputs, dict):
        return paths
    for node in outputs.values():
        if not isinstance(node, dict):
            continue
        for key in OUTPUT_KEYS:
            files = node.get(key)
            if not isinstance(files, list):
                continue
            for item in files:
                if isinstance(item, dict) and item.get("type") == "output" and item.get("filename"):
                    paths.append(str(Path(base, item.get("subfolder") or "", item["filename"])))
    return paths


def _error_message(entry: dict) -> str:
    """The exception_message of the prompt's execution_error, else 'execution failed'."""
    status = entry.get("status")
    messages = status.get("messages") if isinstance(status, dict) else None
    for message in messages if isinstance(messages, list) else []:
        if isinstance(message, list) and len(message) == 2 and message[0] == "execution_error":
            data = message[1]
            text = data.get("exception_message") if isinstance(data, dict) else None
            if text:
                return str(text)
    return "execution failed"


def wait(
    cfg: ComfyCfg,
    prompt_id: str,
    timeout_s: float,
    cancelled: Callable[[], bool],
    sleep: Callable[[float], None] = time.sleep,
    now: Callable[[], float] = time.monotonic,
    alive: Callable[[], bool] | None = None,
) -> dict:
    """Poll /history/<id> every 1.5 s until the prompt ends.

    Returns {"status": "done" | "error" | "timeout" | "cancelled", "seconds", "outputs", "error"}.
    outputs (done only) are the saved files' paths. When cancelled() turns true, a prompt that is
    still unfinished is interrupted (POST /interrupt) and "cancelled" is returned; a timeout leaves
    the prompt running. alive, when given, says whether the ComfyUI process still runs: once it
    returns False on a poll, the prompt did not finish and the result is "error" ("comfyui process
    ended") at once, with any outputs the history already lists.
    """
    t0 = now()

    def result(status: str, outputs: list[str] | None = None, error: str | None = None) -> dict:
        return {"status": status, "seconds": now() - t0, "outputs": outputs or [], "error": error}

    history_url = f"{cfg.url.rstrip('/')}/history/{prompt_id}"
    while True:
        code, body = http.http_json(history_url)
        entry = body.get(prompt_id) if code == 200 and isinstance(body, dict) else None
        if isinstance(entry, dict):
            state = entry.get("status")
            state = state if isinstance(state, dict) else {}
            if state.get("status_str") == "error":
                return result("error", error=_error_message(entry))
            if state.get("completed"):
                return result("done", _output_paths(cfg, entry))
        if alive is not None and not alive():
            outputs = _output_paths(cfg, entry) if isinstance(entry, dict) else []
            return result("error", outputs, "comfyui process ended")
        if cancelled():
            http.http_json(cfg.url.rstrip("/") + "/interrupt", "POST")
            return result("cancelled")
        if now() - t0 >= timeout_s:
            return result("timeout")
        sleep(WAIT_POLL_S)


def free(cfg: ComfyCfg) -> bool:
    """Ask ComfyUI to unload its models and free its memory; True when it said OK."""
    code, _ = http.http_json(
        cfg.url.rstrip("/") + "/free",
        "POST",
        {"unload_models": True, "free_memory": True},
        timeout=FREE_TIMEOUT_S,
    )
    return code == 200


def delete_pending(cfg: ComfyCfg, prompt_ids: list[str]) -> bool:
    """Remove queued prompts that have not started (POST /queue {"delete": [...]}); True when
    ComfyUI said OK. Nothing is sent, and True returned, for an empty list. A prompt that is
    already running is not affected: that is what /interrupt (see wait) is for."""
    if not prompt_ids:
        return True
    code, _ = http.http_json(cfg.url.rstrip("/") + "/queue", "POST", {"delete": list(prompt_ids)})
    return code == 200


# --- stopping ----------------------------------------------------------------------------------


def stop(
    cfg: ComfyCfg,
    proc: subprocess.Popen | None,
    log: Log,
    sleep: Callable[[float], None] | None = None,
) -> dict:
    """End ComfyUI and everything it started.

    The process is proc when given, else whatever listens on the port of cfg.url. How it is ended
    (and how long it is given) is procs.stop_pids's rule.

    Returns {"stopped", "pids"}: pids are the processes aimed at (empty when nothing was running,
    which is not "stopped"); stopped is True when there was at least one and kill_tree reports every
    one gone.
    """
    pids = [proc.pid] if proc is not None else procs.listener_pids(procs.url_port(cfg.url))
    if not pids:
        log("comfyui: nothing to stop")
        return {"stopped": False, "pids": []}
    log(f"comfyui: stopping pids {pids}")
    stopped = procs.stop_pids(pids, sleep, proc)
    log("comfyui: stopped" if stopped else f"comfyui: could not stop pids {pids}")
    return {"stopped": stopped, "pids": pids}
