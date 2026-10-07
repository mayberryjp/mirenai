from collections.abc import Iterator

import pytest
from dnslib import DNSRecord
from webtest import TestApp

from mirenai import config, db
from mirenai.api.app import create_app
from mirenai.api.routes import upstreams as routes
from mirenai.domain import upstream_probe
from mirenai.domain.state import UpstreamServer
from mirenai.repository import upstreams as repo


@pytest.fixture()
def temp_config_db(tmp_path, monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    for _name in ("config", "stats", "cache", "querylog"):
        monkeypatch.setattr(
            config.settings, f"{_name}_database_url", f"sqlite:///{tmp_path / f'{_name}.db'}"
        )
    db.create_all_schemas()
    yield


def test_probe_upstream_returns_rtt(monkeypatch: pytest.MonkeyPatch) -> None:
    reply = DNSRecord.question("example.com", "A").reply().pack()

    def _fake_send(self: object, dest: str, port: int, tcp: bool = False, timeout: float | None = None) -> bytes:
        return reply

    monkeypatch.setattr(DNSRecord, "send", _fake_send)
    server = UpstreamServer(address="8.8.8.8", port=53, protocol="udp", priority=100)
    rtt = upstream_probe.probe_upstream(server, timeout=5.0)
    assert isinstance(rtt, float)
    assert rtt >= 0.0


def test_probe_upstream_raises_on_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    def _fake_send(self: object, dest: str, port: int, tcp: bool = False, timeout: float | None = None) -> bytes:
        raise OSError("network unreachable")

    monkeypatch.setattr(DNSRecord, "send", _fake_send)
    server = UpstreamServer(address="10.255.255.1", port=53, protocol="udp", priority=100)
    with pytest.raises(upstream_probe.ProbeError):
        upstream_probe.probe_upstream(server, timeout=0.1)


def test_check_upstream_success(temp_config_db: None, monkeypatch: pytest.MonkeyPatch) -> None:
    created = repo.create_upstream({"address": "8.8.8.8"})
    monkeypatch.setattr(routes, "probe_upstream", lambda server, timeout: 12.3)
    app = TestApp(create_app())
    resp = app.post(f"/upstreams/{created['id']}/check")
    assert resp.status_code == 200
    assert resp.json == {"status": "ok", "rtt_ms": 12.3}


def test_check_upstream_not_found(temp_config_db: None) -> None:
    app = TestApp(create_app())
    resp = app.post("/upstreams/99999/check", status=404)
    assert resp.json["code"] == "not_found"


def test_check_upstream_probe_failure(temp_config_db: None, monkeypatch: pytest.MonkeyPatch) -> None:
    created = repo.create_upstream({"address": "8.8.8.8"})

    def _raise(server: UpstreamServer, timeout: float) -> float:
        raise upstream_probe.ProbeError("timed out")

    monkeypatch.setattr(routes, "probe_upstream", _raise)
    app = TestApp(create_app())
    resp = app.post(f"/upstreams/{created['id']}/check", status=502)
    assert resp.json["code"] == "upstream_error"
    assert resp.json["detail"] == "timed out"
