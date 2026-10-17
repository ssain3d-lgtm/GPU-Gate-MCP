import pytest

from fakes import FakeClock, FakeComfy, FakeStrata, FakeVoice
from gpu_gate.config import ComfyCfg, StrataCfg, VoiceCfg

API_KEY = "test-key"


@pytest.fixture
def clock():
    """A fake clock: pass clock.sleep and clock.now to the adapters; clock.sleeps records the waits."""
    return FakeClock()


@pytest.fixture
def strata_factory():
    """Build (FakeStrata, StrataCfg) pairs from FakeStrata keyword arguments; all are stopped at the end."""
    started: list[FakeStrata] = []

    def make(**kwargs):
        fake = FakeStrata(**kwargs)
        started.append(fake)
        return fake, StrataCfg(url=fake.start(), api_key=API_KEY)

    yield make
    for fake in started:
        fake.stop()


@pytest.fixture
def fake_strata(strata_factory):
    return strata_factory()


@pytest.fixture
def fake_strata_busy_2(strata_factory):
    return strata_factory(busy_times=2)


@pytest.fixture
def fake_strata_unloaded(strata_factory):
    return strata_factory(loaded=False)


@pytest.fixture
def fake_strata_load_fails(strata_factory):
    return strata_factory(loaded=False, load_fails=True)


@pytest.fixture
def comfy_factory(tmp_path):
    """Build (FakeComfy, ComfyCfg) pairs from FakeComfy keyword arguments; all are stopped at the end.

    The config points at tmp_path as the ComfyUI folder, so the default output folder is
    tmp_path/ComfyUI/output.
    """
    started: list[FakeComfy] = []

    def make(**kwargs):
        fake = FakeComfy(**kwargs)
        started.append(fake)
        return fake, ComfyCfg(dir=str(tmp_path), bat="Start ComfyUI.bat", url=fake.start())

    yield make
    for fake in started:
        fake.stop()


@pytest.fixture
def fake_comfy(comfy_factory):
    return comfy_factory()


@pytest.fixture
def fake_comfy_reject(comfy_factory):
    return comfy_factory(reject=True)


@pytest.fixture
def voice_factory(tmp_path):
    """Build (FakeVoice, VoiceCfg) pairs from FakeVoice keyword arguments; all are stopped at the end.

    The config points at tmp_path as the VoiceStudio folder; the scripts are left unset.
    """
    started: list[FakeVoice] = []

    def make(**kwargs):
        fake = FakeVoice(**kwargs)
        started.append(fake)
        return fake, VoiceCfg(dir=str(tmp_path), url=fake.start())

    yield make
    for fake in started:
        fake.stop()


@pytest.fixture
def fake_voice(voice_factory):
    return voice_factory()
