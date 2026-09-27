from collections.abc import Iterator
from datetime import datetime, timedelta

import pytest
from webtest import TestApp

from mirenai import config, db
from mirenai.api.app import create_app
from mirenai.db import Base, session_scope
from mirenai.repository import client_requests as repo
from mirenai.repository.models import ClientRequest, QueryLog


@pytest.fixture()
def temp_config_db(tmp_path, monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    db_path = tmp_path / "mirenai.db"
    monkeypatch.setattr(config.settings, "database_url", f"sqlite:///{db_path}")
    # Force the cached engine/session factory to rebuild against the temp DB.
    monkeypatch.setattr(db, "_engine", None)
    monkeypatch.setattr(db, "_session_factory", None)
    Base.metadata.create_all(db.get_engine())
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


def test_recent_new_domains_route_envelope_and_limit(monkeypatch: pytest.MonkeyPatch) -> None:
    captured: dict[str, int] = {}

    def _fake(limit: int = 100) -> list[dict[str, object]]:
        captured["limit"] = limit
        return [{"client": "10.0.0.5", "domain": "a.com", "first_seen": "2026-09-27T09:00:00"}]

    monkeypatch.setattr(repo, "list_recent_new_domains", _fake)
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
