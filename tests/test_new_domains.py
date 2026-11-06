from collections.abc import Iterator
from datetime import datetime, timedelta

import pytest
from webtest import TestApp

from mirenai import config, db
from mirenai.api.app import create_app
from mirenai.db import session_scope
from mirenai.domain.clientrequests import ClientRequestAgg, ClientRequestBuffer, ClientRequestFlush
from mirenai.domain.querybuffer import QueryBuffer
from mirenai.domain.state import RuntimeSettings, RuntimeState
from mirenai.repository import blocklists as blocklists_repo
from mirenai.repository import client_requests as repo
from mirenai.repository import hosts as hosts_repo
from mirenai.repository import query_log as query_log_repo
from mirenai.repository.models import ClientRequest, QueryLog
from mirenai.workers import dns_server


@pytest.fixture()
def temp_config_db(tmp_path, monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    for _name in ("config", "stats", "cache", "querylog"):
        monkeypatch.setattr(
            config.settings, f"{_name}_database_url", f"sqlite:///{tmp_path / f'{_name}.db'}"
        )
    db.create_all_schemas()
    yield


def _add(client: str, domain: str, qtype: str, first_seen: datetime) -> None:
    with session_scope() as session:
        session.add(
            ClientRequest(
                client=client,
                domain=domain,
                qtype=qtype,
                hits=1,
                first_seen=first_seen,
                last_seen=first_seen,
            )
        )


def _add_query(
    client: str, domain: str, qtype: str, last_action: str, last_seen: datetime
) -> None:
    with session_scope() as session:
        session.add(
            QueryLog(
                client=client,
                domain=domain,
                qtype=qtype,
                count=1,
                last_action=last_action,
                first_seen=last_seen,
                last_seen=last_seen,
            )
        )


def test_new_domain_counts_buckets_by_hour(temp_config_db: None) -> None:
    now = datetime.now()
    h1 = now - timedelta(hours=1, minutes=10)
    h2 = now - timedelta(hours=2, minutes=10)
    old = now - timedelta(hours=25)
    _add("10.0.0.5", "a.com", "A", h1)
    _add("10.0.0.5", "a.com", "AAAA", h1)  # same domain, other qtype -> counted once
    _add("10.0.0.5", "b.com", "A", h1)
    _add("10.0.0.5", "c.com", "A", h2)
    _add("10.0.0.9", "a.com", "A", h1)  # different client, same domain
    _add("10.0.0.5", "old.com", "A", old)  # outside the 20h window

    repo.materialize_new_domains()
    rows = repo.list_new_domain_counts()

    bucket1 = h1.strftime("%Y-%m-%dT%H:00:00")
    bucket2 = h2.strftime("%Y-%m-%dT%H:00:00")
    old_bucket = old.strftime("%Y-%m-%dT%H:00:00")
    counts = {(r["hour_start"], r["client"]): r["new_domains"] for r in rows}
    assert counts[(bucket1, "10.0.0.5")] == 2
    assert counts[(bucket1, "10.0.0.9")] == 1
    assert counts[(bucket2, "10.0.0.5")] == 1
    # dense: both known clients get a row for every hour in the window (zeros included)
    clients = {r["client"] for r in rows}
    assert clients == {"10.0.0.5", "10.0.0.9"}
    for c in clients:
        assert sum(1 for r in rows if r["client"] == c) == repo.NEW_DOMAIN_WINDOW_HOURS
    assert counts[(bucket2, "10.0.0.9")] == 0  # explicit zero-fill
    assert all(hour != old_bucket for hour, _ in counts)  # out-of-window hour never written


def test_new_domain_counts_client_filter(temp_config_db: None) -> None:
    now = datetime.now()
    h1 = now - timedelta(hours=1, minutes=10)
    _add("10.0.0.5", "a.com", "A", h1)
    _add("10.0.0.9", "b.com", "A", h1)
    repo.materialize_new_domains()
    rows = repo.list_new_domain_counts(client="10.0.0.5")
    assert all(r["client"] == "10.0.0.5" for r in rows)
    assert len(rows) == repo.NEW_DOMAIN_WINDOW_HOURS  # dense series for the one client
    assert sum(r["new_domains"] for r in rows) == 1


def test_new_domains_route_envelope_and_filter(monkeypatch: pytest.MonkeyPatch) -> None:
    captured: dict[str, str | None] = {}

    def _fake(client: str | None = None) -> list[dict[str, object]]:
        captured["client"] = client
        return [{"hour_start": "2026-09-27T09:00:00", "client": "10.0.0.5", "new_domains": 3}]

    monkeypatch.setattr(repo, "list_new_domain_counts", _fake)
    app = TestApp(create_app())

    resp = app.get("/stats/new-domains")
    assert resp.status_code == 200
    assert resp.json["status"] == "ok"
    assert resp.json["total"] == 1
    assert resp.json["stats"][0]["new_domains"] == 3
    assert captured["client"] is None

    app.get("/stats/new-domains?client=10.0.0.5")
    assert captured["client"] == "10.0.0.5"


def test_recent_new_domains_orders_by_first_seen(temp_config_db: None) -> None:
    now = datetime.now()
    _add("10.0.0.5", "old.com", "A", now - timedelta(hours=3))
    _add("10.0.0.5", "a.com", "A", now - timedelta(hours=2))
    _add("10.0.0.5", "a.com", "AAAA", now - timedelta(hours=1))  # same domain, later qtype
    _add("10.0.0.9", "new.com", "A", now - timedelta(minutes=5))

    rows = repo.list_recent_new_domains()

    # newest first_seen first; (client, domain) collapsed to its earliest first_seen
    assert [(r["client"], r["domain"]) for r in rows] == [
        ("10.0.0.9", "new.com"),
        ("10.0.0.5", "a.com"),
        ("10.0.0.5", "old.com"),
    ]
    a_com = next(r for r in rows if r["domain"] == "a.com")
    assert a_com["first_seen"] == (now - timedelta(hours=2)).isoformat()


def test_recent_new_domains_respects_limit(temp_config_db: None) -> None:
    now = datetime.now()
    for i in range(5):
        _add("10.0.0.5", f"d{i}.com", "A", now - timedelta(minutes=i))

    rows = repo.list_recent_new_domains(limit=3)

    assert len(rows) == 3
    assert [r["domain"] for r in rows] == ["d0.com", "d1.com", "d2.com"]


@pytest.mark.parametrize("flush_seconds", [5, 17])
def test_worker_recent_domains_use_query_flush_interval(
    monkeypatch: pytest.MonkeyPatch, flush_seconds: int
) -> None:
    state = RuntimeState(
        load_settings=lambda: RuntimeSettings(query_flush_seconds=flush_seconds),
        load_policies=list,
        load_upstreams=list,
        load_blocklist=frozenset,
        load_blocklist_excluded=frozenset,
        load_trusted_networks=list,
        load_local_records=list,
    )
    monkeypatch.setattr(dns_server, "_build_state", lambda: state)
    monkeypatch.setattr(dns_server, "seed_seen_domains", lambda: None)
    monkeypatch.setattr(dns_server, "configure_logging", lambda level: None)

    class StartupChecked(Exception):
        pass

    class CheckedRequests(ClientRequestBuffer):
        def __init__(self, flush: ClientRequestFlush, flush_seconds: int) -> None:
            assert flush is repo.record_client_requests
            assert flush_seconds == state.settings.query_flush_seconds
            raise StartupChecked

    monkeypatch.setattr(dns_server, "ClientRequestBuffer", CheckedRequests)
    with pytest.raises(StartupChecked):
        dns_server.main()


def test_recent_route_includes_domain_after_request_flush(temp_config_db: None) -> None:
    db.BlocklistBase.metadata.create_all(db.get_blocklist_engine())
    db.HostsBase.metadata.create_all(db.get_hosts_engine())
    queries = QueryBuffer(flush=query_log_repo.record_queries, flush_seconds=5)
    requests = ClientRequestBuffer(flush=repo.record_client_requests, flush_seconds=5)
    app = TestApp(create_app())
    domain = "mosquitto.office.example.com"
    for qtype in ("A", "AAAA"):
        queries.add("10.0.0.5", domain, qtype, "forward", "10.0.0.2")
        requests.add("10.0.0.5", domain, qtype)
    queries.flush()
    requests.flush()

    host_queries = app.get("/queries?client=10.0.0.5").json["queries"]
    recent = app.get("/stats/new-domains/recent").json["domains"]
    assert {row["domain"] for row in host_queries} == {domain}
    assert len(recent) == 1
    assert recent[0]["client"] == "10.0.0.5"
    assert recent[0]["domain"] == domain
    assert recent[0]["last_action"] == "forward"
    first_seen = recent[0]["first_seen"]

    requests.add("10.0.0.5", domain, "A")
    requests.flush()
    assert app.get("/stats/new-domains/recent").json["domains"][0]["first_seen"] == first_seen
    assert repo.list_new_domain_counts() == []  # no hourly materialization needed


def test_recent_route_first_seen_is_per_client_domain_pair(temp_config_db: None) -> None:
    db.BlocklistBase.metadata.create_all(db.get_blocklist_engine())
    db.HostsBase.metadata.create_all(db.get_hosts_engine())
    now = datetime.now()
    older = now - timedelta(hours=3)
    newer = now - timedelta(minutes=5)
    _add("10.0.0.5", "shared.com", "A", older)
    _add("10.0.0.9", "shared.com", "AAAA", newer)
    _add("10.0.0.9", "shared.com", "A", now)
    repo.record_client_requests([
        ClientRequestAgg(client="10.0.0.9", domain="shared.com", qtype="AAAA", hits=2),
    ])
    _add_query("10.0.0.5", "shared.com", "A", "deny", now)
    _add_query("10.0.0.9", "shared.com", "AAAA", "forward", newer)
    app = TestApp(create_app())

    response = app.get("/stats/new-domains/recent").json

    assert response["status"] == "ok"
    assert response["total"] == 2
    assert [
        (row["client"], row["domain"], row["first_seen"], row["last_action"])
        for row in response["domains"]
    ] == [
        ("10.0.0.9", "shared.com", newer.isoformat(), "forward"),
        ("10.0.0.5", "shared.com", older.isoformat(), "deny"),
    ]
    limited = app.get("/stats/new-domains/recent?limit=1").json
    assert limited["total"] == 1
    assert limited["domains"] == response["domains"][:1]


def test_recent_new_domains_includes_last_action(temp_config_db: None) -> None:
    now = datetime.now()
    _add("10.0.0.5", "a.com", "A", now - timedelta(hours=2))
    _add("10.0.0.5", "a.com", "AAAA", now - timedelta(hours=1))
    _add("10.0.0.5", "b.com", "A", now - timedelta(minutes=5))  # no query-log row
    # newest last_seen across qtypes wins for the collapsed (client, domain) row
    _add_query("10.0.0.5", "a.com", "A", "forward", now - timedelta(hours=2))
    _add_query("10.0.0.5", "a.com", "AAAA", "blocklist", now - timedelta(minutes=1))

    rows = repo.list_recent_new_domains()
    actions = {r["domain"]: r["last_action"] for r in rows}

    assert actions["a.com"] == "blocklist"
    assert actions["b.com"] is None
    qtypes = {r["domain"]: r["last_qtype"] for r in rows}
    assert qtypes["a.com"] == "AAAA"
    assert qtypes["b.com"] is None


def test_recent_new_domains_route_envelope_and_limit(monkeypatch: pytest.MonkeyPatch) -> None:
    captured: dict[str, int] = {}

    def _fake(limit: int = 100, exclude_clients: object = None) -> list[dict[str, object]]:
        captured["limit"] = limit
        return [{"client": "10.0.0.5", "domain": "a.com", "first_seen": "2026-09-27T09:00:00"}]

    monkeypatch.setattr(repo, "list_recent_new_domains", _fake)
    # Isolate the envelope from the blocklist DB (annotation is covered in test_blocklist_match).
    monkeypatch.setattr(blocklists_repo, "annotate_blocked", lambda rows, key="domain": rows)
    # Isolate from the hosts DB; exclusion is covered in test_blocklist_match.
    monkeypatch.setattr(hosts_repo, "load_new_domain_unmonitored", frozenset)
    app = TestApp(create_app())

    resp = app.get("/stats/new-domains/recent")
    assert resp.status_code == 200
    assert resp.json["status"] == "ok"
    assert resp.json["total"] == 1
    assert resp.json["domains"][0]["domain"] == "a.com"
    assert captured["limit"] == 100  # default "top recent 100"

    app.get("/stats/new-domains/recent?limit=25")
    assert captured["limit"] == 25

    resp = app.get("/stats/new-domains/recent?limit=oops", status=422)
    assert resp.status_code == 422
