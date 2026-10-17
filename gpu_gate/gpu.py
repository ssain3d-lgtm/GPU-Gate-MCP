"""GPU usage queries: total VRAM in use, and VRAM per process.

Standard library only. `nvidia-smi --query-compute-apps` reports per-process memory as N/A under
Windows WDDM, so the per-process figures come from the `GPU Process Memory` performance counters,
read through PowerShell. Every query answers None (or "") instead of raising when it cannot read.
"""

from __future__ import annotations

import re
import time
from collections.abc import Callable

from . import procs

MIN_PROCESS_MIB = 300  # smaller users (the desktop, browsers) are not worth reporting
COUNTER_TIMEOUT_S = 20
POLL_S = 1.0

_COUNTER_SCRIPT = (
    r"(Get-Counter '\GPU Process Memory(*)\Dedicated Usage').CounterSamples"
    r" | ForEach-Object { $_.InstanceName + '|' + [int64]$_.CookedValue }"
)
# pid_12356_luid_0x00000000_0x0000f1a2_phys_0|17667000000
_COUNTER_LINE = re.compile(r"^pid_(\d+)_\S*\|(\d+)$")


def _run_nvidia_smi() -> str:
    """Raw `nvidia-smi` answer for the used memory of each GPU (MiB, one per line); "" on failure."""
    return procs._run_quiet(
        ["nvidia-smi", "--query-gpu=memory.used", "--format=csv,noheader,nounits"]
    )


def vram_used_mib() -> int | None:
    """VRAM in use on the first GPU, in MiB; None when nvidia-smi is missing or does not answer."""
    lines = _run_nvidia_smi().strip().splitlines()
    try:
        return int(lines[0].strip())
    except (IndexError, ValueError):
        return None


def _run_counter() -> str:
    """Raw `GPU Process Memory` counter text, one `instance|bytes` line per sample; "" on failure."""
    if not procs.IS_WINDOWS:
        return ""
    return procs._run_quiet(
        ["powershell", "-NoProfile", "-NonInteractive", "-Command", _COUNTER_SCRIPT],
        timeout=COUNTER_TIMEOUT_S,
    )


def process_vram(run: Callable[[], str] | None = None) -> list[dict] | None:
    """Processes holding at least 300 MiB of dedicated VRAM, biggest first.

    Each entry is {"pid": int, "name": str | None, "vram_mb": int}. One process shows up as several
    counter instances (one per adapter and engine), which are added together: the 300 MiB cut-off
    is applied to the exact byte total, while vram_mb adds up the instances after each was cut to
    whole MiB (at most one MiB short per extra instance). None when the counter text is empty or
    has no line that can be read.
    """
    text = (run or _run_counter)()
    used_bytes: dict[int, int] = {}
    used_mib: dict[int, int] = {}
    for line in text.splitlines():
        match = _COUNTER_LINE.match(line.strip())
        if match:
            pid, size = int(match.group(1)), int(match.group(2))
            used_bytes[pid] = used_bytes.get(pid, 0) + size
            used_mib[pid] = used_mib.get(pid, 0) + size // 2**20
    if not used_bytes:
        return None
    found = [
        {"pid": pid, "name": procs.process_name(pid), "vram_mb": used_mib[pid]}
        for pid, size in used_bytes.items()
        if size >= MIN_PROCESS_MIB * 2**20
    ]
    found.sort(key=lambda entry: entry["vram_mb"], reverse=True)
    return found


def _vram_released(pid: int) -> bool:
    listing = process_vram()
    if listing is None:  # the counter is unreadable: a process that is gone holds no VRAM
        return not procs.pid_alive(pid)
    return all(entry["pid"] != pid for entry in listing)


def pid_vram_gone(
    pid: int,
    timeout_s: float,
    sleep: Callable[[float], None] = time.sleep,
    now: Callable[[], float] = time.monotonic,
) -> bool:
    """True once pid no longer holds VRAM (checked every second), False if still there after timeout_s."""
    deadline = now() + timeout_s
    while True:
        if _vram_released(pid):
            return True
        if now() >= deadline:
            return False
        sleep(POLL_S)
