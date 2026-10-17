import json
import os

import pytest

from gpu_gate import config, state
from gpu_gate.__main__ import main


@pytest.fixture
def home(tmp_path, monkeypatch):
    monkeypatch.setenv(state.ENV_HOME, str(tmp_path / "home"))
    # Recorded so the value main() sets is undone after the test.
    monkeypatch.setenv(config.ENV_CONFIG, "unset-by-the-test")
    monkeypatch.delenv(config.ENV_CONFIG)
    return state.home()


def test_init_writes_the_example_config_once(home, capsys):
    target = config.config_path(home)

    assert main(["--selftest"]) == 1
    err = capsys.readouterr().err
    assert "config not found" in err and len(err.strip().splitlines()) == 1

    assert main(["--init"]) == 0
    assert str(target) in capsys.readouterr().out
    assert json.loads(target.read_text(encoding="utf-8")) == config.EXAMPLE

    target.write_text("{}", encoding="utf-8")
    assert main(["--init"]) == 0
    assert capsys.readouterr().out.strip() == f"config exists: {target}"
    assert target.read_text(encoding="utf-8") == "{}"  # never overwritten


def test_selftest_reports_probes_and_tools(home, tmp_path, monkeypatch, capsys, fake_strata, fake_comfy, fake_voice):
    _, strata_cfg = fake_strata
    _, comfy_cfg = fake_comfy
    _, voice_cfg = fake_voice
    path = tmp_path / "elsewhere" / "gate.json"
    path.parent.mkdir()
    path.write_text(json.dumps({
        "strata": {"url": strata_cfg.url, "api_key": "test-key"},
        "comfyui": {"command": ["python", "main.py"], "url": comfy_cfg.url},
        "voicestudio": {"url": voice_cfg.url},
    }), encoding="utf-8")

    assert main(["--config", str(path), "--selftest"]) == 0

    assert os.environ[config.ENV_CONFIG] == str(path)
    out = capsys.readouterr().out
    assert str(path) in out
    assert str(home) in out
    assert "loaded" in out
    assert out.count("ready") >= 2
    for name in ("comfy_batch", "voice_batch", "gate_status"):
        assert name in out
