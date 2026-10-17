"""Command line: the MCP stdio server by default, plus --worker, --selftest, --init and --config.

Serving, stdout carries only protocol lines; every diagnostic goes to stderr.
"""

from __future__ import annotations

import argparse
import os
import sys

from . import comfyui, config, state, strata, voicestudio


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="gpu_gate_server.py",
        description="GPU-Gate MCP stdio server (no switch: serve on stdin/stdout).",
    )
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--worker", metavar="BATCH_JSON", help="run one batch file (started by the server)")
    mode.add_argument("--selftest", action="store_true", help="check the config, the servers and the protocol")
    mode.add_argument("--init", action="store_true", help="write the example config if there is none")
    parser.add_argument("--config", metavar="PATH", help="use this config file (sets GPU_GATE_CONFIG)")
    return parser


def _init(home) -> int:
    path = config.config_path(home)
    try:
        config.write_example(path)
    except FileExistsError:
        print(f"config exists: {path}")
        return 0
    print(f"wrote the example config: {path}")
    print("edit it for this machine, then run: python gpu_gate_server.py --selftest")
    return 0


def _selftest(home) -> int:
    from .server import Server  # here, so --worker never imports the server

    try:
        cfg = config.load(home)
    except config.ConfigError as err:
        print(f"gpu-gate: {' '.join(str(err).split())}", file=sys.stderr)
        return 1

    health = strata.health(cfg.strata)
    if health is None:
        strata_text = "absent (no answer)"
    else:
        strata_text = "loaded" if health.get("loaded") else "not loaded"
    rows = [
        ("config", cfg.path),
        ("home", str(home)),
        ("strata", f"{cfg.strata.url}  {strata_text}"),
        ("comfyui", f"{cfg.comfyui.url}  {'ready' if comfyui.ready(cfg.comfyui) else 'down'}"),
        ("voicestudio", f"{cfg.voicestudio.url}  {'ready' if voicestudio.ready(cfg.voicestudio) else 'down'}"),
    ]

    def no_worker(batch_path: str) -> int:
        raise RuntimeError("the selftest starts no worker")

    server = Server(home, lambda: cfg, no_worker)
    init = server.handle({"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}})
    listed = server.handle({"jsonrpc": "2.0", "id": 2, "method": "tools/list"})
    names = [tool["name"] for tool in listed["result"]["tools"]]
    rows.append(("mcp", f"protocol {init['result']['protocolVersion']}, tools: {', '.join(names)}"))

    width = max(len(key) for key, _ in rows)
    for key, value in rows:
        print(f"{key.ljust(width)}  {value}")
    return 0


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    if args.config:
        os.environ[config.ENV_CONFIG] = os.path.abspath(args.config)
    if args.worker:
        from . import worker

        return worker.main(args.worker)
    home = state.home()
    if args.init:
        return _init(home)
    if args.selftest:
        return _selftest(home)

    from .server import make_server

    make_server(home).serve(sys.stdin.buffer, sys.stdout.buffer)
    return 0


if __name__ == "__main__":
    sys.exit(main())
