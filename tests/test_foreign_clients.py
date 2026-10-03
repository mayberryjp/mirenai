from collections.abc import Iterator
from datetime import datetime, timedelta

import pytest
from webtest import TestApp

from mirenai import config, db
from mirenai.api.app import create_app
from mirenai.db import Base, session_scope
from mirenai.domain.foreignclients import ForeignClientAgg, ForeignClientBuffer
from mirenai.repository import foreign_clients as repo
from mirenai.repository.models import ForeignClient


@pytest.fixture()
def temp_config_db(tmp_path, monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    db_path = tmp_path / "mirenai.db"
    monkeypatch.setattr(config.settings, "database_url", f"sqlite:///{db_path}")
    # Force the cached engine/session factory to rebuild against the temp DB.
    monkeypatch.setattr(db, "_engine", None)
    monkeypatch.setattr(db, "_session_factory", None)
    Base.metadata.create_all(db.get_engine())
    yield


def _insert(ip: str, hits: int, last_seen: datetime) -> None:
    with session_scope() as session:
        session.add(ForeignClient(ip=ip, hits=hits, first_seen=last_seen, last_seen=last_seen))


# --- buffer ---------------------------------------------------------------


def test_buffer_aggregates_hits_per_ip() -> None:
    captured: list[ForeignClientAgg] = []
    buf = ForeignClientBuffer(flush=captured.extend, flush_seconds=3600)
    buf.add("1.1.1.1")
    buf.add("1.1.1.1")
    buf.add("2.2.2.2")
    buf.flush()
    assert {row.ip: row.hits for row in captured} == {"1.1.1.1": 2, "2.2.2.2": 1}


def test_buffer_caps_distinct_ips() -> None:
    captured: list[ForeignClientAgg] = []
    buf = ForeignClientBuffer(flush=captured.extend, flush_seconds=3600, max_clients=2)
    buf.add("1.1.1.1")
    buf.add("2.2.2.2")
    buf.add("3.3.3.3")  # dropped: cap reached and this IP is new
    buf.add("1.1.1.1")  # still counts: already tracked
    buf.flush()
    agg = {row.ip: row.hits for row in captured}
    assert agg == {"1.1.1.1": 2, "2.2.2.2": 1}
    assert "3.3.3.3" not in agg


def test_buffer_flush_empty_is_noop() -> None:
    captured: list[ForeignClientAgg] = []
    buf = ForeignClientBuffer(flush=captured.extend, flush_seconds=3600)
    buf.flush()
    assert captured == []


# --- repository -----------------------------------------------------------


def test_record_upserts_hits(temp_config_db: None) -> None:
    repo.record_foreign_clients([ForeignClientAgg(ip="9.9.9.9", hits=2)])
    repo.record_foreign_clients([ForeignClientAgg(ip="9.9.9.9", hits=3)])
    rows = repo.list_foreign_clients()
    assert len(rows) == 1
    assert rows[0]["ip"] == "9.9.9.9"
    assert rows[0]["hits"] == 5


def test_record_empty_is_noop(temp_config_db: None) -> None:
    repo.record_foreign_clients([])
    assert repo.count_foreign_clients() == 0


def test_list_orders_by_last_seen_desc(temp_config_db: None) -> None:
    now = datetime.now()
    _insert("1.1.1.1", 5, now - timedelta(minutes=5))
    _insert("2.2.2.2", 1, now - timedelta(minutes=1))
    rows = repo.list_foreign_clients()
    assert [row["ip"] for row in rows] == ["2.2.2.2", "1.1.1.1"]
    page = repo.list_foreign_clients(limit=1, offset=0)
    assert [row["ip"] for row in page] == ["2.2.2.2"]


def test_record_prunes_to_cap(temp_config_db: None, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(repo, "MAX_FOREIGN_CLIENTS", 2)
    now = datetime.now()
    _insert("1.1.1.1", 1, now - timedelta(hours=3))
    _insert("2.2.2.2", 1, now - timedelta(hours=2))
    _insert("3.3.3.3", 1, now - timedelta(hours=1))
    # Inserting a newer IP triggers the prune to the 2 most recent by last_seen.
    repo.record_foreign_clients([ForeignClientAgg(ip="4.4.4.4", hits=1)])
    assert [row["ip"] for row in repo.list_foreign_clients()] == ["4.4.4.4", "3.3.3.3"]
    assert repo.count_foreign_clients() == 2


# --- route ----------------------------------------------------------------


def test_foreign_clients_route_envelope(temp_config_db: None) -> None:
    repo.record_foreign_clients([ForeignClientAgg(ip="7.7.7.7", hits=4)])
    app = TestApp(create_app())
    resp = app.get("/foreign-clients")
    assert resp.status_code == 200
    assert resp.json["status"] == "ok"
    assert resp.json["total"] == 1
    assert resp.json["foreign_clients"][0]["ip"] == "7.7.7.7"
    assert resp.json["foreign_clients"][0]["hits"] == 4


def test_foreign_clients_route_pagination(temp_config_db: None) -> None:
    now = datetime.now()
    _insert("1.1.1.1", 1, now - timedelta(minutes=3))
    _insert("2.2.2.2", 1, now - timedelta(minutes=2))
    _insert("3.3.3.3", 1, now - timedelta(minutes=1))
    app = TestApp(create_app())
    resp = app.get("/foreign-clients?limit=2&offset=0")
    assert resp.json["total"] == 3
    assert [row["ip"] for row in resp.json["foreign_clients"]] == ["3.3.3.3", "2.2.2.2"]
