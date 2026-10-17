import pytest

from fakes import FakeClock, FakeStrata
from gpu_gate.config import StrataCfg

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
