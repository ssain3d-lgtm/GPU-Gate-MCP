from gpu_gate import gpu

COUNTER_TEXT = (
    "pid_12356_luid_0x0000_phys_0|17667000000\n"
    "pid_12356_luid_0x0001_phys_0|1000000\n"
    "pid_2596_luid_0x0_phys_0|8300000000\n"
    "pid_77_luid_0x0_phys_0|1000\n"
)


def test_process_vram_parses_counter_lines(monkeypatch):
    monkeypatch.setattr(gpu, "_run_counter", lambda: COUNTER_TEXT)
    monkeypatch.setattr(
        gpu.procs, "process_name", lambda pid: {12356: "python", 2596: "dwm"}.get(pid)
    )
    # 12356 holds 17667000000 + 1000000 bytes: 17,668,000,000 // 1,048,576 = 16849 (bytes are added before flooring)
    assert gpu.process_vram() == [
        {"pid": 12356, "name": "python", "vram_mb": 16849},
        {"pid": 2596, "name": "dwm", "vram_mb": 7915},
    ]


def test_process_vram_sorts_by_size_and_keeps_an_unknown_name_as_none(monkeypatch):
    text = "pid_5_luid_0x0_phys_0|400000000\npid_6_luid_0x0_phys_0|900000000\n"
    monkeypatch.setattr(gpu.procs, "process_name", lambda pid: None)
    result = gpu.process_vram(run=lambda: text)
    assert result == [
        {"pid": 6, "name": None, "vram_mb": 858},
        {"pid": 5, "name": None, "vram_mb": 381},
    ]


def test_process_vram_returns_none_when_counter_fails(monkeypatch):
    monkeypatch.setattr(gpu, "_run_counter", lambda: "")
    assert gpu.process_vram() is None
    # text with no parsable line is just as unreadable as no text
    assert gpu.process_vram(run=lambda: "Get-Counter : the counter path is not valid\n") is None


def test_process_vram_is_empty_not_none_when_every_process_is_small(monkeypatch):
    monkeypatch.setattr(gpu, "_run_counter", lambda: "pid_77_luid_0x0_phys_0|1000\n")
    assert gpu.process_vram() == []


def test_vram_used_parses_nvidia_smi(monkeypatch):
    monkeypatch.setattr(gpu, "_run_nvidia_smi", lambda: "27431\n")
    assert gpu.vram_used_mib() == 27431
    monkeypatch.setattr(gpu, "_run_nvidia_smi", lambda: "27431\n 512\n")  # several GPUs: the first
    assert gpu.vram_used_mib() == 27431


def test_vram_used_is_none_when_nvidia_smi_fails(monkeypatch):
    for text in ("", "\n", "NVIDIA-SMI has failed because it couldn't communicate\n"):
        monkeypatch.setattr(gpu, "_run_nvidia_smi", lambda text=text: text)
        assert gpu.vram_used_mib() is None


def test_run_counter_calls_powershell_quietly_on_windows(monkeypatch):
    calls = []
    monkeypatch.setattr(gpu.procs, "IS_WINDOWS", True)
    monkeypatch.setattr(
        gpu.procs, "_run_quiet", lambda argv, timeout=30: calls.append((argv, timeout)) or "out"
    )
    assert gpu._run_counter() == "out"
    argv, timeout = calls[0]
    assert argv[:4] == ["powershell", "-NoProfile", "-NonInteractive", "-Command"]
    assert "GPU Process Memory(*)" in argv[4] and "Dedicated Usage" in argv[4]
    assert timeout == 20


def test_run_counter_is_empty_off_windows(monkeypatch):
    def boom(argv, timeout=30):
        raise AssertionError("must not run a command")

    monkeypatch.setattr(gpu.procs, "IS_WINDOWS", False)
    monkeypatch.setattr(gpu.procs, "_run_quiet", boom)
    assert gpu._run_counter() == ""


def test_pid_vram_gone_polls_every_second_until_the_pid_leaves(monkeypatch, clock):
    seen = [
        [{"pid": 9, "name": "python", "vram_mb": 5000}],
        [{"pid": 9, "name": "python", "vram_mb": 4000}, {"pid": 3, "name": "x", "vram_mb": 900}],
        [{"pid": 3, "name": "x", "vram_mb": 900}],
    ]
    monkeypatch.setattr(gpu, "process_vram", lambda: seen.pop(0))
    assert gpu.pid_vram_gone(9, 30, sleep=clock.sleep, now=clock.now) is True
    assert clock.sleeps == [1.0, 1.0]


def test_pid_vram_gone_gives_up_after_the_timeout(monkeypatch, clock):
    monkeypatch.setattr(gpu, "process_vram", lambda: [{"pid": 9, "name": None, "vram_mb": 5000}])
    assert gpu.pid_vram_gone(9, 3, sleep=clock.sleep, now=clock.now) is False
    assert clock.sleeps == [1.0, 1.0, 1.0]


def test_pid_vram_gone_falls_back_to_pid_liveness_without_the_counter(monkeypatch, clock):
    monkeypatch.setattr(gpu, "process_vram", lambda: None)
    alive = iter([True, False])
    monkeypatch.setattr(gpu.procs, "pid_alive", lambda pid: next(alive))
    assert gpu.pid_vram_gone(9, 30, sleep=clock.sleep, now=clock.now) is True
    assert clock.sleeps == [1.0]
