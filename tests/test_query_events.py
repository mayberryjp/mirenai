from collections.abc import Iterator
from datetime import datetime, timedelta

import pytest
from sqlalchemy import select
from webtest import TestApp

from mirenai import config, db
from mirenai.api.app import create_app
from mirenai.db import session_scope
from mirenai.domain.queryevents import QueryEvent
from mirenai.repository import query_events as repo
from mirenai.repository.models import ClientQueryEvent


@pytest.fixture()
def temp_config_db(tmp_path, monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    for _name in ("config", "stats", "cache", "querylog"):
        monkeypatch.setattr(
            config.settings, f"{_name}_database_url", f"sqlite:///{tmp_path / f'{_name}.db'}"
        )
    db.create_all_schemas()
    yield


def _add(
    client: str,
    domain: str,
    created_at: datetime,
    qtype: str = "A",
    rcode: str = "NOERROR",
    response: str = "",
) -> None:
    with session_scope() as session:
        session.add(
            ClientQueryEvent(
                client=client,
                domain=domain,
                qtype=qtype,
                rcode=rcode,
                response=response,
                created_at=created_at,
            )
        )


def test_list_recent_queries_filters_by_window(temp_config_db: None) -> None:
    now = datetime.now()
    _add("10.0.0.5", "recent.example", now - timedelta(seconds=10), response="1.2.3.4")
    _add("10.0.0.5", "old.example", now - timedelta(seconds=120))
    rows = repo.list_recent_queries("10.0.0.5", seconds=60)
    assert [r["domain"] for r in rows] == ["recent.example"]
    assert rows[0]["response"] == "1.2.3.4"
    assert rows[0]["timestamp"]


def test_list_recent_queries_filters_by_client(temp_config_db: None) -> None:
    now = datetime.now()
    _add("10.0.0.5", "a.example", now - timedelta(seconds=5))
    _add("10.0.0.9", "b.example", now - timedelta(seconds=5))
    rows = repo.list_recent_queries("10.0.0.5", seconds=60)
    assert [r["client"] for r in rows] == ["10.0.0.5"]


def test_list_recent_queries_orders_newest_first_and_limits(temp_config_db: None) -> None:
    now = datetime.now()
    _add("10.0.0.5", "first.example", now - timedelta(seconds=30))
    _add("10.0.0.5", "second.example", now - timedelta(seconds=20))
    _add("10.0.0.5", "third.example", now - timedelta(seconds=10))
    rows = repo.list_recent_queries("10.0.0.5", seconds=60, limit=2)
    assert [r["domain"] for r in rows] == ["third.example", "second.example"]


def test_record_query_events_inserts_and_prunes(temp_config_db: None) -> None:
    now = datetime.now()
    _add("10.0.0.5", "stale.example", now - timedelta(seconds=repo.RETENTION_SECONDS + 60))
    repo.record_query_events(
        [
            QueryEvent(
                client="10.0.0.5",
                domain="fresh.example",
                qtype="A",
                rcode="NOERROR",
                response="9.9.9.9",
                created_at=now,
            )
        ]
    )
    with session_scope() as session:
        domains = {row.domain for row in session.scalars(select(ClientQueryEvent)).all()}
    assert domains == {"fresh.example"}


def test_recent_queries_endpoint_returns_events(temp_config_db: None) -> None:
    now = datetime.now()
    _add(
        "10.0.0.5",
        "recent.example",
        now - timedelta(seconds=10),
        rcode="NOERROR",
        response="1.2.3.4",
    )
    app = TestApp(create_app())
    resp = app.get("/clients/10.0.0.5/queries?seconds=60")
    assert resp.status_code == 200
    body = resp.json
    assert body["status"] == "ok"
    assert body["client"] == "10.0.0.5"
    assert body["seconds"] == 60
    assert body["total"] == 1
    row = body["queries"][0]
    assert row["domain"] == "recent.example"
    assert row["qtype"] == "A"
    assert row["rcode"] == "NOERROR"
    assert row["response"] == "1.2.3.4"
    assert row["timestamp"]


def test_recent_queries_endpoint_defaults_seconds(temp_config_db: None) -> None:
    app = TestApp(create_app())
    resp = app.get("/clients/10.0.0.5/queries")
    assert resp.status_code == 200
    assert resp.json["seconds"] == 60


def test_recent_queries_endpoint_rejects_bad_ip(temp_config_db: None) -> None:
    app = TestApp(create_app())
    resp = app.get("/clients/not-an-ip/queries", status=422)
    assert resp.json["code"] == "validation_error"


def test_recent_queries_endpoint_rejects_non_positive_seconds(temp_config_db: None) -> None:
    app = TestApp(create_app())
    resp = app.get("/clients/10.0.0.5/queries?seconds=0", status=422)
    assert resp.json["code"] == "validation_error"
