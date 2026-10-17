import copy
import json
from pathlib import Path

import pytest

from gpu_gate import config

REPO_ROOT = Path(__file__).resolve().parent.parent


@pytest.fixture(autouse=True)
def no_config_override(monkeypatch):
    """A developer's own GPU_GATE_CONFIG must not leak into these tests."""
    monkeypatch.delenv("GPU_GATE_CONFIG", raising=False)


@pytest.fixture
def example(tmp_path):
    """The example config with every path pointing at real, fictional files under tmp_path."""
    comfy = tmp_path / "ComfyUI"
    voice = tmp_path / "VoiceStudio"
    comfy.mkdir()
    (voice / "app").mkdir(parents=True)
    (voice / "start_backend.ps1").write_text("", encoding="utf-8")
    (voice / "voice_batch.py").write_text("", encoding="utf-8")
    data = copy.deepcopy(config.EXAMPLE)
    data["comfyui"]["dir"] = str(comfy)
    data["voicestudio"]["dir"] = str(voice / "app")
    data["voicestudio"]["start_script"] = str(voice / "start_backend.ps1")
    data["voicestudio"]["batch_script"] = str(voice / "voice_batch.py")
    return data


def write_config(tmp_path, data):
    path = tmp_path / "config.json"
    path.write_text(json.dumps(data), encoding="utf-8")
    return path


def test_missing_config_names_the_path(tmp_path):
    with pytest.raises(config.ConfigError) as e:
        config.load(tmp_path)
    assert str(tmp_path / "config.json") in str(e.value) and "--init" in str(e.value)
    # Plain ASCII around the path, so a cp949 console with redirected stderr prints it as is.
    assert str(e.value) == f"config not found: {tmp_path / 'config.json'} - run: python gpu_gate_server.py --init"


def test_example_config_loads_when_dirs_exist(tmp_path, example):
    path = write_config(tmp_path, example)
    cfg = config.load(tmp_path)
    assert cfg.path == str(path)
    assert cfg.strata == config.StrataCfg(
        url="http://127.0.0.1:8080", api_key=None, unload_wait_s=300, load_timeout_s=400
    )
    assert cfg.comfyui == config.ComfyCfg(
        dir=example["comfyui"]["dir"],
        bat="Start ComfyUI.bat",
        command=None,
        url="http://127.0.0.1:8188",
        start_timeout_s=300,
        output_dir=None,
    )
    assert cfg.voicestudio == config.VoiceCfg(
        dir=example["voicestudio"]["dir"],
        start_script=example["voicestudio"]["start_script"],
        batch_script=example["voicestudio"]["batch_script"],
        url="http://127.0.0.1:3900",
        start_timeout_s=180,
    )
    assert cfg.limits == config.Limits(job_timeout_s=1800, batch_timeout_s=9000)
    assert cfg.other_llm_ports == [8081]


def test_comfyui_needs_bat_or_command(tmp_path, example):
    example["comfyui"]["bat"] = None
    example["comfyui"]["command"] = None
    path = write_config(tmp_path, example)
    with pytest.raises(config.ConfigError) as e:
        config.load(tmp_path)
    msg = str(e.value)
    assert "comfyui.bat" in msg and "comfyui.command" in msg and str(path) in msg


def test_bad_dir_is_reported_with_key_and_file(tmp_path, example):
    missing = str(tmp_path / "no-such-dir")
    example["comfyui"]["dir"] = missing
    path = write_config(tmp_path, example)
    with pytest.raises(config.ConfigError) as e:
        config.load(tmp_path)
    assert str(e.value) == f"comfyui.dir does not exist: {missing} ({path})"


def test_env_var_overrides_config_path(tmp_path, monkeypatch, example):
    elsewhere = tmp_path / "elsewhere" / "my-gate.json"
    elsewhere.parent.mkdir()
    elsewhere.write_text(json.dumps(example), encoding="utf-8")
    monkeypatch.setenv("GPU_GATE_CONFIG", str(elsewhere))
    assert config.config_path(tmp_path) == elsewhere
    assert config.load(tmp_path).path == str(elsewhere)


def test_config_path_defaults_to_config_json_in_home(tmp_path):
    assert config.config_path(tmp_path) == tmp_path / "config.json"


def test_missing_sections_use_defaults(tmp_path):
    write_config(tmp_path, {"comfyui": {"command": ["python", "main.py"]}})
    cfg = config.load(tmp_path)
    assert cfg.comfyui.command == ["python", "main.py"]
    assert cfg.comfyui.dir is None and cfg.comfyui.bat is None
    assert cfg.comfyui.url == "http://127.0.0.1:8188"
    assert cfg.strata.url == "http://127.0.0.1:8080" and cfg.strata.api_key is None
    assert (cfg.strata.unload_wait_s, cfg.strata.load_timeout_s) == (300, 400)
    assert cfg.comfyui.start_timeout_s == 300
    assert cfg.voicestudio.dir is None and cfg.voicestudio.start_timeout_s == 180
    assert cfg.limits == config.Limits(job_timeout_s=1800, batch_timeout_s=9000)
    assert cfg.other_llm_ports == []


def test_config_without_a_comfyui_section_is_rejected(tmp_path):
    write_config(tmp_path, {"strata": {"url": "http://127.0.0.1:8080"}})
    with pytest.raises(config.ConfigError) as e:
        config.load(tmp_path)
    assert "comfyui.bat" in str(e.value) and "comfyui.command" in str(e.value)


def test_unknown_keys_are_ignored(tmp_path, example):
    example["future_section"] = {"x": 1}
    example["strata"]["future_key"] = True
    write_config(tmp_path, example)
    assert config.load(tmp_path).strata.url == "http://127.0.0.1:8080"


def test_utf8_bom_is_tolerated(tmp_path, example):
    (tmp_path / "config.json").write_text(json.dumps(example), encoding="utf-8-sig")
    assert config.load(tmp_path).comfyui.bat == "Start ComfyUI.bat"


def test_invalid_json_names_the_file(tmp_path):
    path = tmp_path / "config.json"
    path.write_text("{ not json", encoding="utf-8")
    with pytest.raises(config.ConfigError) as e:
        config.load(tmp_path)
    assert str(e.value).startswith(f"config is not valid JSON: {path} (")


def test_top_level_must_be_an_object(tmp_path):
    path = write_config(tmp_path, [1, 2])
    with pytest.raises(config.ConfigError) as e:
        config.load(tmp_path)
    assert str(path) in str(e.value) and "object" in str(e.value)


def test_section_must_be_an_object(tmp_path, example):
    example["limits"] = 5
    path = write_config(tmp_path, example)
    with pytest.raises(config.ConfigError) as e:
        config.load(tmp_path)
    assert "limits" in str(e.value) and str(path) in str(e.value)


def test_comfyui_command_is_enough_without_bat_or_dir(tmp_path, example):
    example["comfyui"].update(dir=None, bat=None, command=["python", "main.py", "--listen"])
    write_config(tmp_path, example)
    cfg = config.load(tmp_path)
    assert cfg.comfyui.command == ["python", "main.py", "--listen"] and cfg.comfyui.dir is None


def test_comfyui_bat_needs_dir(tmp_path, example):
    example["comfyui"]["dir"] = None
    path = write_config(tmp_path, example)
    with pytest.raises(config.ConfigError) as e:
        config.load(tmp_path)
    assert "comfyui.dir" in str(e.value) and "comfyui.bat" in str(e.value)
    assert str(path) in str(e.value)


@pytest.mark.parametrize("command", ["python main.py", [], ["python", 3], ["python", ""]])
def test_comfyui_command_must_be_a_list_of_strings(tmp_path, example, command):
    example["comfyui"]["command"] = command
    path = write_config(tmp_path, example)
    with pytest.raises(config.ConfigError) as e:
        config.load(tmp_path)
    assert "comfyui.command" in str(e.value) and str(path) in str(e.value)


@pytest.mark.parametrize("key", ["start_script", "batch_script", "dir"])
def test_voicestudio_paths_must_exist_when_given(tmp_path, example, key):
    missing = str(tmp_path / "gone")
    example["voicestudio"][key] = missing
    path = write_config(tmp_path, example)
    with pytest.raises(config.ConfigError) as e:
        config.load(tmp_path)
    assert str(e.value) == f"voicestudio.{key} does not exist: {missing} ({path})"


def test_voicestudio_paths_may_be_null(tmp_path, example):
    example["voicestudio"].update(dir=None, start_script=None, batch_script=None)
    write_config(tmp_path, example)
    assert config.load(tmp_path).voicestudio.start_script is None


@pytest.mark.parametrize(
    "section,key,value",
    [
        ("strata", "unload_wait_s", 0),
        ("strata", "load_timeout_s", -5),
        ("strata", "unload_wait_s", "300"),
        ("strata", "unload_wait_s", 1.5),
        ("strata", "unload_wait_s", True),
        ("comfyui", "start_timeout_s", None),
        ("voicestudio", "start_timeout_s", False),
        ("limits", "job_timeout_s", 0),
        ("limits", "batch_timeout_s", "9000"),
    ],
)
def test_numbers_must_be_positive_integers(tmp_path, example, section, key, value):
    example[section][key] = value
    path = write_config(tmp_path, example)
    with pytest.raises(config.ConfigError) as e:
        config.load(tmp_path)
    assert f"{section}.{key}" in str(e.value) and str(path) in str(e.value)


@pytest.mark.parametrize(
    "section,value",
    [("strata", "127.0.0.1:8080"), ("comfyui", "ftp://host"), ("voicestudio", 3900), ("strata", None)],
)
def test_urls_must_be_http(tmp_path, example, section, value):
    example[section]["url"] = value
    path = write_config(tmp_path, example)
    with pytest.raises(config.ConfigError) as e:
        config.load(tmp_path)
    assert f"{section}.url" in str(e.value) and str(path) in str(e.value)


def test_https_urls_are_accepted(tmp_path, example):
    example["strata"]["url"] = "https://llm.example.test"
    write_config(tmp_path, example)
    assert config.load(tmp_path).strata.url == "https://llm.example.test"


def test_api_key_may_be_a_string(tmp_path, example):
    example["strata"]["api_key"] = "not-a-real-key"
    write_config(tmp_path, example)
    assert config.load(tmp_path).strata.api_key == "not-a-real-key"


@pytest.mark.parametrize("key,section", [("api_key", "strata"), ("output_dir", "comfyui")])
def test_optional_strings_must_be_strings(tmp_path, example, key, section):
    example[section][key] = 12
    path = write_config(tmp_path, example)
    with pytest.raises(config.ConfigError) as e:
        config.load(tmp_path)
    assert f"{section}.{key}" in str(e.value) and str(path) in str(e.value)


@pytest.mark.parametrize("ports", [8081, ["8081"], [True], [0], [70000], [8081, 1.5]])
def test_other_llm_ports_must_be_a_list_of_ports(tmp_path, example, ports):
    example["other_llm_ports"] = ports
    path = write_config(tmp_path, example)
    with pytest.raises(config.ConfigError) as e:
        config.load(tmp_path)
    assert "other_llm_ports" in str(e.value) and str(path) in str(e.value)


def test_write_example_writes_a_loadable_template(tmp_path):
    target = tmp_path / "nested" / "config.json"
    config.write_example(target)
    assert json.loads(target.read_text(encoding="utf-8")) == config.EXAMPLE


def test_write_example_refuses_to_overwrite(tmp_path):
    target = tmp_path / "config.json"
    target.write_text("mine", encoding="utf-8")
    with pytest.raises(FileExistsError):
        config.write_example(target)
    assert target.read_text(encoding="utf-8") == "mine"


def test_example_file_matches_constant():
    text = (REPO_ROOT / "config.example.json").read_text(encoding="utf-8")
    assert json.loads(text) == config.EXAMPLE
    assert text == json.dumps(config.EXAMPLE, indent=2) + "\n"
