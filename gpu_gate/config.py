"""Configuration: the one config.json that holds every machine-specific value.

Loaded from <home>/config.json (or the file named by GPU_GATE_CONFIG) and validated up front, so
a typo fails with a message that names the key and the file instead of surfacing mid-batch.
Unknown keys are ignored (forward compatibility); a missing section uses its defaults.

Standard library only.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path

from . import state

ENV_CONFIG = "GPU_GATE_CONFIG"

# The template written by `--init`; the repo's config.example.json is this, serialized.
EXAMPLE: dict = {
    "strata": {
        "url": "http://127.0.0.1:8080",
        "api_key": None,
        "unload_wait_s": 300,
        "load_timeout_s": 400,
    },
    "comfyui": {
        "dir": "C:/ComfyUI",
        "bat": "Start ComfyUI.bat",
        "command": None,
        "url": "http://127.0.0.1:8188",
        "start_timeout_s": 300,
        "output_dir": None,
    },
    "voicestudio": {
        "dir": "C:/VoiceStudio/app",
        "start_script": "C:/VoiceStudio/start_backend.ps1",
        "batch_script": "C:/VoiceStudio/voice_batch.py",
        "url": "http://127.0.0.1:3900",
        "start_timeout_s": 180,
    },
    "limits": {
        "job_timeout_s": 1800,
        "batch_timeout_s": 9000,
    },
    "other_llm_ports": [8081],
}


class ConfigError(Exception):
    """The config is missing or wrong; the message names the key and the file."""


@dataclass(frozen=True)
class StrataCfg:
    url: str = "http://127.0.0.1:8080"
    api_key: str | None = None
    unload_wait_s: int = 300
    load_timeout_s: int = 400


@dataclass(frozen=True)
class ComfyCfg:
    dir: str | None = None
    bat: str | None = None
    command: list[str] | None = None
    url: str = "http://127.0.0.1:8188"
    start_timeout_s: int = 300
    output_dir: str | None = None


@dataclass(frozen=True)
class VoiceCfg:
    dir: str | None = None
    start_script: str | None = None
    batch_script: str | None = None
    url: str = "http://127.0.0.1:3900"
    start_timeout_s: int = 180


@dataclass(frozen=True)
class Limits:
    job_timeout_s: int = 1800
    batch_timeout_s: int = 9000


@dataclass(frozen=True)
class Config:
    strata: StrataCfg
    comfyui: ComfyCfg
    voicestudio: VoiceCfg
    limits: Limits
    other_llm_ports: list[int]
    path: str  # the file this was loaded from


def config_path(home: Path | str) -> Path:
    """GPU_GATE_CONFIG if set, else <home>/config.json."""
    override = os.environ.get(ENV_CONFIG)
    return Path(override) if override else state.paths(home).config


def write_example(path: Path | str) -> None:
    """Write the example config to path (creating folders); never overwrite an existing file."""
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    with open(target, "x", encoding="utf-8", newline="\n") as f:  # "x": FileExistsError
        f.write(json.dumps(EXAMPLE, indent=2) + "\n")


# --- validation ------------------------------------------------------------------------------
# A check takes (config file, "section.key", value) and returns the value or raises ConfigError.


def _error(file: Path, message: str) -> ConfigError:
    return ConfigError(f"{message} ({file})")


def _positive_int(file: Path, key: str, value):
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise _error(file, f"{key} must be a positive integer: {value!r}")
    return value


def _url(file: Path, key: str, value):
    if not isinstance(value, str) or not value.startswith(("http://", "https://")):
        raise _error(file, f"{key} must be a URL starting with http:// or https://: {value!r}")
    return value


def _optional_str(file: Path, key: str, value):
    if value is not None and (not isinstance(value, str) or not value):
        raise _error(file, f"{key} must be a non-empty string or null: {value!r}")
    return value


def _existing_path(file: Path, key: str, value):
    if value is not None and not Path(value).exists():
        raise _error(file, f"{key} does not exist: {value}")
    return value


def _optional_existing_path(file: Path, key: str, value):
    return _existing_path(file, key, _optional_str(file, key, value))


def _command(file: Path, key: str, value):
    ok = isinstance(value, list) and value and all(isinstance(a, str) and a for a in value)
    if value is not None and not ok:
        raise _error(file, f"{key} must be a non-empty list of non-empty strings or null: {value!r}")
    return value


def _ports(file: Path, key: str, value):
    ok = isinstance(value, list) and all(
        isinstance(p, int) and not isinstance(p, bool) and 0 < p < 65536 for p in value
    )
    if not ok:
        raise _error(file, f"{key} must be a list of port numbers (1-65535): {value!r}")
    return value


def _section(file: Path, data: dict, name: str) -> dict:
    raw = data.get(name, {})
    if not isinstance(raw, dict):
        raise _error(file, f"{name} must be an object: {raw!r}")
    return raw


def _checked(file: Path, name: str, raw: dict, checks: dict) -> dict:
    """The keys of raw that are present, each validated; absent keys fall back to the defaults."""
    return {key: check(file, f"{name}.{key}", raw[key]) for key, check in checks.items() if key in raw}


def _strata(file: Path, data: dict) -> StrataCfg:
    checks = {
        "url": _url,
        "api_key": _optional_str,
        "unload_wait_s": _positive_int,
        "load_timeout_s": _positive_int,
    }
    return StrataCfg(**_checked(file, "strata", _section(file, data, "strata"), checks))


def _comfyui(file: Path, data: dict) -> ComfyCfg:
    checks = {
        "dir": _optional_str,
        "bat": _optional_str,
        "command": _command,
        "url": _url,
        "start_timeout_s": _positive_int,
        "output_dir": _optional_str,
    }
    cfg = ComfyCfg(**_checked(file, "comfyui", _section(file, data, "comfyui"), checks))
    if cfg.bat is None and cfg.command is None:
        raise _error(file, "comfyui.bat and comfyui.command are both missing: set one of them")
    if cfg.bat is not None and cfg.dir is None:
        raise _error(file, "comfyui.dir is required when comfyui.bat is set")
    _existing_path(file, "comfyui.dir", cfg.dir)
    return cfg


def _voicestudio(file: Path, data: dict) -> VoiceCfg:
    checks = {
        "dir": _optional_existing_path,
        "start_script": _optional_existing_path,
        "batch_script": _optional_existing_path,
        "url": _url,
        "start_timeout_s": _positive_int,
    }
    return VoiceCfg(**_checked(file, "voicestudio", _section(file, data, "voicestudio"), checks))


def _limits(file: Path, data: dict) -> Limits:
    checks = {"job_timeout_s": _positive_int, "batch_timeout_s": _positive_int}
    return Limits(**_checked(file, "limits", _section(file, data, "limits"), checks))


def load(home: Path | str) -> Config:
    """Read and validate the config; raises ConfigError naming the key and the file."""
    file = config_path(home)
    try:
        text = file.read_text(encoding="utf-8-sig")  # tolerate a BOM from Windows editors
    except FileNotFoundError:
        raise ConfigError(
            f"config not found: {file} - run: python gpu_gate_server.py --init"
        ) from None
    except (OSError, ValueError) as err:
        raise ConfigError(f"config could not be read: {file} ({err})") from err
    try:
        data = json.loads(text)
    except ValueError as err:
        raise ConfigError(f"config is not valid JSON: {file} ({err})") from err
    if not isinstance(data, dict):
        raise _error(file, f"config must be a JSON object: {type(data).__name__}")

    return Config(
        strata=_strata(file, data),
        comfyui=_comfyui(file, data),
        voicestudio=_voicestudio(file, data),
        limits=_limits(file, data),
        other_llm_ports=list(_ports(file, "other_llm_ports", data.get("other_llm_ports", []))),
        path=str(file),
    )
