import dataclasses

from fakes import FakeServer
from gpu_gate import http, strata
from gpu_gate.config import StrataCfg


def quiet(msg):
    assert "test-key" not in msg  # the api key must never reach the log


def test_pause_unloads_and_waits_for_loaded_false(fake_strata, clock):
    fake, cfg = fake_strata
    r = strata.pause(cfg, quiet, sleep=clock.sleep, now=clock.now)
    assert r == {"status": "unloaded", "paused_s": 0.0, "tries": 1}
    assert fake.loaded is False
    assert ("POST", "/unload?wait=1") in fake.calls


def test_pause_polls_health_until_the_engine_is_gone(strata_factory, clock):
    fake, cfg = strata_factory(health_lag=3)
    r = strata.pause(cfg, quiet, sleep=clock.sleep, now=clock.now)
    assert r["status"] == "unloaded" and r["paused_s"] == 3.0
    assert clock.sleeps == [1, 1, 1]
    assert [c for c in fake.calls if c[1] == "/health"] == [("GET", "/health")] * 5  # 1 probe + 4 polls


def test_pause_says_not_loaded_when_nothing_was_loaded(strata_factory, clock):
    fake, cfg = strata_factory(loaded=False)
    r = strata.pause(cfg, quiet, sleep=clock.sleep, now=clock.now)
    assert r["status"] == "not loaded" and r["tries"] == 1 and clock.sleeps == []


def test_pause_retries_busy_every_60s_then_succeeds(fake_strata_busy_2, clock):
    fake, cfg = fake_strata_busy_2
    r = strata.pause(cfg, quiet, sleep=clock.sleep, now=clock.now)
    assert r["status"] == "unloaded" and r["tries"] == 3
    assert clock.sleeps == [60, 60]
    assert r["paused_s"] == 120.0
    assert fake.loaded is False


def test_pause_gives_up_after_900s_of_busy(strata_factory, clock):
    fake, cfg = strata_factory(busy_times=10_000)
    r = strata.pause(cfg, quiet, sleep=clock.sleep, now=clock.now)
    assert r["status"] == "busy" and r["tries"] == 16
    assert clock.sleeps == [60] * 15
    assert r["paused_s"] == 900.0
    assert fake.loaded is True


def test_pause_stops_on_unexpected_answer(strata_factory, clock):
    fake, cfg = strata_factory(unload_status=401)
    r = strata.pause(cfg, quiet, sleep=clock.sleep, now=clock.now)
    assert r["status"] == "error" and r["tries"] == 1
    assert "401" in r["error"] and "nope" in r["error"] and "test-key" not in r["error"]
    assert clock.sleeps == []
    assert fake.loaded is True
    assert [c for c in fake.calls if c[1].startswith("/unload")] == [("POST", "/unload?wait=1")]


def test_pause_stops_on_a_server_error_after_a_busy_answer(strata_factory, clock):
    fake, cfg = strata_factory(busy_times=1, unload_status=500)
    r = strata.pause(cfg, quiet, sleep=clock.sleep, now=clock.now)
    assert r["status"] == "error" and r["tries"] == 2 and "500" in r["error"]
    assert clock.sleeps == [60]


def test_pause_reports_absent_when_nothing_answers(clock):
    cfg = StrataCfg(url="http://127.0.0.1:1")
    r = strata.pause(cfg, quiet, sleep=clock.sleep, now=clock.now)
    assert r == {"status": "absent", "paused_s": 0.0, "tries": 0}
    assert clock.sleeps == []


def test_resume_loads_and_waits(fake_strata_unloaded, clock):
    fake, cfg = fake_strata_unloaded
    r = strata.resume(cfg, quiet, sleep=clock.sleep, now=clock.now)
    assert r == {"restored": True, "load_s": 0.0, "error": None, "already_loaded": False}
    assert fake.loaded is True
    assert ("POST", "/load") in fake.calls


def test_resume_polls_health_every_half_second_until_loaded(strata_factory, clock):
    fake, cfg = strata_factory(loaded=False, health_lag=4)
    r = strata.resume(cfg, quiet, sleep=clock.sleep, now=clock.now)
    assert r["restored"] is True and r["already_loaded"] is False and r["load_s"] == 2.0
    assert clock.sleeps == [0.5] * 4


def test_resume_is_a_noop_when_already_loaded(fake_strata, clock):
    fake, cfg = fake_strata
    r = strata.resume(cfg, quiet, sleep=clock.sleep, now=clock.now)
    assert r == {"restored": True, "load_s": 0.0, "error": None, "already_loaded": True}
    assert [c[0] for c in fake.calls] == ["GET"]  # health only, no /load


def test_resume_retries_three_times_then_reports_error(fake_strata_load_fails, clock):
    fake, cfg = fake_strata_load_fails
    r = strata.resume(cfg, quiet, sleep=clock.sleep, now=clock.now)
    assert r["restored"] is False and r["already_loaded"] is False
    assert "load" in r["error"] and "503" in r["error"] and "gpu busy" in r["error"]
    assert [c for c in fake.calls if c[1] == "/load"] == [("POST", "/load")] * 3
    assert clock.sleeps == [15, 15]
    assert fake.loaded is False


def test_resume_times_out_when_the_engine_never_comes_up(strata_factory, clock):
    fake, base = strata_factory(loaded=False, health_lag=10_000)
    cfg = dataclasses.replace(base, load_timeout_s=5)
    r = strata.resume(cfg, quiet, sleep=clock.sleep, now=clock.now)
    assert r["restored"] is False and "timed out after 5 s" in r["error"]
    assert len([c for c in fake.calls if c[1] == "/load"]) == 3
    assert clock.sleeps.count(15) == 2


def test_resume_reports_when_strata_does_not_answer(monkeypatch, clock):
    monkeypatch.setattr(http, "http_json", lambda *args, **kwargs: (0, None))  # a dead port is slow on Windows
    r = strata.resume(StrataCfg(), quiet, sleep=clock.sleep, now=clock.now)
    assert r["restored"] is False and "no answer" in r["error"]
    assert clock.sleeps == [15, 15]


def test_health_and_status_return_the_servers_json(fake_strata):
    fake, cfg = fake_strata
    assert strata.health(cfg)["loaded"] is True
    assert strata.status(cfg)["busy"] is False


def test_health_and_status_are_none_when_nothing_answers(monkeypatch):
    monkeypatch.setattr(http, "http_json", lambda *args, **kwargs: (0, None))
    assert strata.health(StrataCfg()) is None
    assert strata.status(StrataCfg()) is None


def test_health_is_none_for_an_error_status(strata_factory):
    fake, cfg = strata_factory()
    assert strata.health(dataclasses.replace(cfg, url=cfg.url + "/wrong")) is None


def test_http_json_sends_json_content_type_and_bearer(fake_strata):
    fake, cfg = fake_strata
    status, body = http.http_json(cfg.url + "/unload?wait=1", "POST", api_key="test-key")
    assert (status, body) == (200, {"status": "unloaded"})
    assert fake.headers["Content-Type"] == "application/json"
    assert fake.headers["Authorization"] == "Bearer test-key"
    assert "Origin" not in fake.headers
    assert fake.last_body == b"{}"


def test_http_json_sends_the_body_as_json_and_omits_auth_without_a_key(fake_strata):
    fake, cfg = fake_strata
    http.http_json(cfg.url + "/load", "POST", body={"a": 1})
    assert fake.last_body == b'{"a": 1}'
    assert "Authorization" not in fake.headers


def test_http_json_returns_the_status_and_body_of_http_errors(strata_factory):
    fake, cfg = strata_factory(busy_times=1)
    assert http.http_json(cfg.url + "/unload?wait=1", "POST") == (409, {"status": "busy"})
    assert http.http_json(cfg.url + "/nope") == (404, {"error": "no route: GET /nope"})


def test_http_json_gives_status_0_when_nothing_answers():
    assert http.http_json("http://127.0.0.1:1/health") == (0, None)


def test_http_json_gives_none_for_a_body_that_is_not_json():
    class Html(FakeServer):
        def handle(self, method, path, body):
            return (200, b"<html>nope</html>") if path == "/page" else (204, b"")

    fake = Html()
    try:
        base = fake.start()
        assert http.http_json(base + "/page") == (200, None)
        assert http.http_json(base + "/empty") == (204, None)
    finally:
        fake.stop()
