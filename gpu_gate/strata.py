"""Strata adapter: pause = unload the engine, resume = load it again.

Strata is the local LLM server that holds the GPU. Pausing it frees the VRAM for a batch; the engine
(not the server) is what gets unloaded, so /health keeps answering and reports `loaded`.

Standard library only. `sleep` and `now` are injectable so tests never wait in real time; `log` is a
callable taking one line of text (the api key is never logged).
"""

from __future__ import annotations

import time
from typing import Callable

from . import http
from .config import StrataCfg

BUSY_RETRY_S = 60  # wait between /unload attempts while a request is still running
BUSY_GIVE_UP_S = 900  # stop retrying a busy server after this long
UNLOAD_POLL_S = 1  # how often to check that the engine is really gone
UNLOAD_POLL_MAX_S = 60
LOAD_POLL_S = 0.5  # how often to check that the engine came up
LOAD_ATTEMPTS = 3
LOAD_RETRY_S = 15  # wait between failed /load attempts
LOAD_REQUEST_TIMEOUT_S = 60

Log = Callable[[str], None]


def _get(cfg: StrataCfg, path: str) -> dict | None:
    code, body = http.http_json(cfg.url.rstrip("/") + path, api_key=cfg.api_key)
    return body if code == 200 and isinstance(body, dict) else None


def health(cfg: StrataCfg) -> dict | None:
    """GET /health as a dict, or None when Strata does not answer."""
    return _get(cfg, "/health")


def status(cfg: StrataCfg) -> dict | None:
    """GET /status as a dict, or None when Strata does not answer."""
    return _get(cfg, "/status")


def _post(cfg: StrataCfg, path: str, timeout: float) -> tuple[int, object | None]:
    return http.http_json(
        cfg.url.rstrip("/") + path, "POST", timeout=timeout, api_key=cfg.api_key
    )


def _short(body: object | None, limit: int = 200) -> str:
    """A body for a log line or error message: one line, cut at limit characters."""
    text = " ".join(str(body).split())
    return text if len(text) <= limit else text[: limit - 3] + "..."


def _detail(code: int, body: object | None) -> str:
    """'HTTP 503 {...}' for the logs and error messages."""
    return f"HTTP {code}" + (f" {_short(body)}" if body else "")


def pause(
    cfg: StrataCfg,
    log: Log,
    sleep: Callable[[float], None] = time.sleep,
    now: Callable[[], float] = time.monotonic,
) -> dict:
    """Unload Strata's engine so the GPU is free.

    Returns {"status": "unloaded" | "not loaded" | "absent" | "busy" | "error", "paused_s", "tries"}
    plus an "error" message when the status is "error".
    "absent" means the first /health probe got no answer: there is nothing to unload or restore.
    Once Strata has answered, a later failure is never "absent" (the engine may be unloaded or not,
    so the caller must still restore it):
    - A server that is running a request answers 409 (or 200 {"status": "busy"}); it is asked again
      every 60 s and given up on ("busy") after 900 s in total.
    - Any other answer (a wrong api key, a server error), or no answer to the /unload request, returns
      at once as "error".
    - After an acknowledged unload, /health is polled every second for up to 60 s until it reports
      loaded false. A lost health answer does not end the poll; if the last answer in those 60 s was
      lost the result is "error"; if /health still says loaded the unload status is returned anyway.
    """
    t0 = now()
    if health(cfg) is None:
        log(f"strata: no answer at {cfg.url}, nothing to unload")
        return {"status": "absent", "paused_s": 0.0, "tries": 0}

    tries = 0
    while True:
        tries += 1
        code, body = _post(cfg, "/unload?wait=1", cfg.unload_wait_s + 30)
        result = body.get("status") if code == 200 and isinstance(body, dict) else None
        if result in ("unloaded", "not loaded"):
            log(f"strata: unload answered '{result}' (try {tries})")
            break
        if code == 0:
            error = "unload request got no answer"
            log(f"strata: {error} (try {tries})")
            return {"status": "error", "error": error, "paused_s": now() - t0, "tries": tries}
        busy = code == 409 or (code == 200 and isinstance(body, dict) and body.get("status") == "busy")
        if not busy:
            error = f"unload answered HTTP {code}" + (f": {_short(body)}" if body else "")
            log(f"strata: {error}")
            return {"status": "error", "error": error, "paused_s": now() - t0, "tries": tries}
        if now() - t0 >= BUSY_GIVE_UP_S:
            log(f"strata: still busy after {now() - t0:.0f} s and {tries} tries, giving up")
            return {"status": "busy", "paused_s": now() - t0, "tries": tries}
        log(f"strata: busy, retrying the unload in {BUSY_RETRY_S} s (try {tries})")
        sleep(BUSY_RETRY_S)

    poll_start = now()
    while True:
        current = health(cfg)
        if current is not None and not current.get("loaded"):
            break
        if now() - poll_start >= UNLOAD_POLL_MAX_S:
            if current is None:
                error = "health stopped answering after unload"
                log(f"strata: {error}")
                return {"status": "error", "error": error, "paused_s": now() - t0, "tries": tries}
            log(f"strata: still reports loaded {UNLOAD_POLL_MAX_S} s after the unload, going on")
            break
        sleep(UNLOAD_POLL_S)
    paused_s = now() - t0
    log(f"strata: unloaded after {paused_s:.0f} s")
    return {"status": result, "paused_s": paused_s, "tries": tries}


def _wait_loaded(
    cfg: StrataCfg, sleep: Callable[[float], None], now: Callable[[], float]
) -> str | None:
    """Poll /health until the engine is loaded; None on success, else why not."""
    start = now()
    while True:
        current = health(cfg)
        if current is not None and current.get("loaded"):
            return None
        if now() - start >= cfg.load_timeout_s:
            return f"load timed out after {cfg.load_timeout_s} s"
        sleep(LOAD_POLL_S)


def resume(
    cfg: StrataCfg,
    log: Log,
    sleep: Callable[[float], None] = time.sleep,
    now: Callable[[], float] = time.monotonic,
) -> dict:
    """Load Strata's engine again; a no-op when it is already loaded.

    Returns {"restored", "load_s", "error", "already_loaded"}. Up to 3 attempts, 15 s apart.
    """
    current = health(cfg)
    if current is not None and current.get("loaded"):
        log("strata: already loaded, nothing to restore")
        return {"restored": True, "load_s": 0.0, "error": None, "already_loaded": True}

    t0 = now()
    for attempt in range(1, LOAD_ATTEMPTS + 1):
        log(f"strata: loading (attempt {attempt}/{LOAD_ATTEMPTS})")
        code, body = _post(cfg, "/load", LOAD_REQUEST_TIMEOUT_S)
        if code == 200:
            reason = _wait_loaded(cfg, sleep, now)
            if reason is None:
                load_s = now() - t0
                log(f"strata: loaded after {load_s:.0f} s")
                return {"restored": True, "load_s": load_s, "error": None, "already_loaded": False}
            error = reason
        elif code == 0:
            error = f"load failed: no answer from Strata at {cfg.url}"
        else:
            error = f"load failed: {_detail(code, body)}"
        log(f"strata: {error}")
        if attempt < LOAD_ATTEMPTS:
            sleep(LOAD_RETRY_S)
    return {"restored": False, "load_s": now() - t0, "error": error, "already_loaded": False}
