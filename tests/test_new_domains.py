from collections.abc import Iterator
from datetime import datetime, timedelta

import pytest
from webtest import TestApp

from mirenai import config, db
from mirenai.api.app import create_app
from mirenai.db import Base, session_scope
from mirenai.repository import client_requests as repo
from mirenai.repository.models import ClientRequest


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
