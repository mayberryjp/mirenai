from collections.abc import Iterator

import pytest
from dnslib import DNSRecord
from sqlalchemy import text
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


def test_tls_upstream_api_round_trip(temp_config_db: None) -> None:
    app = TestApp(create_app())
    created = app.post_json("/upstreams", {
        "address": "1.1.1.1", "use_tls": True, "tls_server_name": "Cloudflare-DNS.com",
    }, status=201).json["upstream"]
    upstream_id = created["id"]
    assert created["port"] == 853
    assert created["use_tls"] is True
    assert created["tls_server_name"] == "cloudflare-dns.com"
    assert app.get(f"/upstreams/{upstream_id}").json["upstream"] == created
    assert app.get("/upstreams").json["upstreams"] == [created]
    server = repo.load_upstreams()[0]
    assert server.use_tls is True
    assert server.tls_server_name == "cloudflare-dns.com"
    assert server.port == 853
    updated = app.put_json(f"/upstreams/{upstream_id}", {"name": "new name"}).json["upstream"]
    assert updated["use_tls"] is True
    assert updated["tls_server_name"] == "cloudflare-dns.com"


@pytest.mark.parametrize("name", [None, "", " ", "https://dns.example", "*.example.com", "1.1.1.1"])
def test_tls_upstream_rejects_missing_or_invalid_name(temp_config_db: None, name: str | None) -> None:
    app = TestApp(create_app())
    response = app.post_json("/upstreams", {
        "address": "1.1.1.1", "use_tls": True, "tls_server_name": name,
    }, status=422)
    assert response.json["code"] == "validation_error"
    assert repo.list_upstreams() == []


def test_tls_upstream_validates_merged_partial_updates(temp_config_db: None) -> None:
    app = TestApp(create_app())
    created = app.post_json("/upstreams", {"address": "1.1.1.1"}, status=201).json["upstream"]
    path = f"/upstreams/{created['id']}"
    assert created["use_tls"] is False
    assert created["tls_server_name"] is None
    assert created["port"] == 53
    app.put_json(path, {"use_tls": True}, status=422)
    assert repo.get_upstream(created["id"]) == created
    app.put_json(path, {"tls_server_name": "cloudflare-dns.com"})
    enabled = app.put_json(path, {"use_tls": True, "port": 853}).json["upstream"]
    assert enabled["use_tls"] is True
    app.put_json(path, {"tls_server_name": None}, status=422)
    app.put_json(path, {"use_tls": None}, status=422)
    disabled = app.put_json(path, {
        "use_tls": False, "tls_server_name": None, "port": 53,
    }).json["upstream"]
    assert disabled["use_tls"] is False
    assert disabled["tls_server_name"] is None


def test_tls_check_uses_persisted_configuration(
    temp_config_db: None, monkeypatch: pytest.MonkeyPatch,
) -> None:
    created = repo.create_upstream({
        "address": "1.1.1.1", "port": 853,
        "use_tls": True, "tls_server_name": "cloudflare-dns.com",
    })
    servers: list[UpstreamServer] = []

    def probe(server: UpstreamServer, timeout: float) -> float:
        servers.append(server)
        return 1.0

    monkeypatch.setattr(routes, "probe_upstream", probe)
    TestApp(create_app()).post(f"/upstreams/{created['id']}/check")
    assert servers[0].use_tls is True
    assert servers[0].tls_server_name == "cloudflare-dns.com"
    assert servers[0].port == 853


def test_probe_dispatches_to_tls_transport(monkeypatch: pytest.MonkeyPatch) -> None:
    servers: list[UpstreamServer] = []

    def query(request: DNSRecord, server: UpstreamServer, timeout: float) -> DNSRecord:
        servers.append(server)
        return request.reply()

    monkeypatch.setattr(upstream_probe, "query_upstream", query)
    server = UpstreamServer("1.1.1.1", 853, "udp", 100, True, "cloudflare-dns.com")
    assert upstream_probe.probe_upstream(server, 5) >= 0
    assert servers == [server]


def test_existing_upstreams_gain_plaintext_defaults_idempotently(temp_config_db: None) -> None:
    with db.get_config_engine().begin() as conn:
        conn.execute(text("DROP TABLE upstreams"))
        conn.execute(text(
            "CREATE TABLE upstreams (id INTEGER PRIMARY KEY, name VARCHAR(128), "
            "address VARCHAR(64) NOT NULL, port INTEGER NOT NULL, protocol VARCHAR(8) NOT NULL, "
            "enabled BOOLEAN NOT NULL, priority INTEGER NOT NULL, "
            "created_at DATETIME DEFAULT CURRENT_TIMESTAMP, "
            "updated_at DATETIME DEFAULT CURRENT_TIMESTAMP)"
        ))
        conn.execute(text(
            "INSERT INTO upstreams (address, port, protocol, enabled, priority) "
            "VALUES ('8.8.8.8', 53, 'udp', 1, 100)"
        ))
    db.init_db()
    db.init_db()
    row = repo.list_upstreams()[0]
    assert row["address"] == "8.8.8.8"
    assert row["port"] == 53
    assert row["use_tls"] is False
    assert row["tls_server_name"] is None
