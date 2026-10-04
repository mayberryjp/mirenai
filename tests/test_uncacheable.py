from collections.abc import Iterator
from datetime import datetime, timedelta

import pytest
from webtest import TestApp

from mirenai import config, db
from mirenai.api.app import create_app
from mirenai.db import Base, session_scope
from mirenai.domain.uncacheable import UncacheableAgg, UncacheableBuffer
from mirenai.repository import uncacheable as repo
from mirenai.repository.models import UncacheableResponse


@pytest.fixture()
def temp_config_db(tmp_path, monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    db_path = tmp_path / "mirenai.db"
    monkeypatch.setattr(config.settings, "database_url", f"sqlite:///{db_path}")
    # Force the cached engine/session factory to rebuild against the temp DB.
    monkeypatch.setattr(db, "_engine", None)
    monkeypatch.setattr(db, "_session_factory", None)
    Base.metadata.create_all(db.get_engine())
    yield


def _insert(
    domain: str,
    qtype: str,
    reason: str,
    hits: int,
    last_seen: datetime,
    last_ttl: int | None = None,
    client: str = "10.0.0.1",
) -> None:
    with session_scope() as session:
        session.add(
            UncacheableResponse(
                client=client,
                domain=domain,
                qtype=qtype,
                reason=reason,
                last_ttl=last_ttl,
                hits=hits,
                first_seen=last_seen,
                last_seen=last_seen,
            )
        )


# --- buffer ---------------------------------------------------------------


def test_buffer_aggregates_hits_per_key() -> None:
    captured: list[UncacheableAgg] = []
    buf = UncacheableBuffer(flush=captured.extend, flush_seconds=3600)
    buf.add("10.0.0.1", "a.example", "A", "nxdomain", None)
    buf.add("10.0.0.1", "a.example", "A", "nxdomain", None)
    buf.add("10.0.0.2", "a.example", "A", "nxdomain", None)  # different client -> own bucket
    buf.add("10.0.0.1", "b.example", "AAAA", "nodata", None)
    buf.flush()
    agg = {(row.client, row.domain, row.qtype, row.reason): row.hits for row in captured}
    assert agg == {
        ("10.0.0.1", "a.example", "A", "nxdomain"): 2,
        ("10.0.0.2", "a.example", "A", "nxdomain"): 1,
        ("10.0.0.1", "b.example", "AAAA", "nodata"): 1,
    }


def test_buffer_keeps_last_ttl() -> None:
    captured: list[UncacheableAgg] = []
    buf = UncacheableBuffer(flush=captured.extend, flush_seconds=3600)
    buf.add("10.0.0.1", "fast.example", "A", "zero-ttl", 0)
    buf.flush()
    assert captured[0].last_ttl == 0


def test_buffer_caps_distinct_keys() -> None:
    captured: list[UncacheableAgg] = []
    buf = UncacheableBuffer(flush=captured.extend, flush_seconds=3600, max_keys=2)
    buf.add("10.0.0.1", "a.example", "A", "nxdomain", None)
    buf.add("10.0.0.1", "b.example", "A", "nxdomain", None)
    buf.add("10.0.0.1", "c.example", "A", "nxdomain", None)  # dropped: cap reached, new key
    buf.add("10.0.0.1", "a.example", "A", "nxdomain", None)  # still counts: already tracked
    buf.flush()
    agg = {row.domain: row.hits for row in captured}
    assert agg == {"a.example": 2, "b.example": 1}
    assert "c.example" not in agg


def test_buffer_flush_empty_is_noop() -> None:
    captured: list[UncacheableAgg] = []
    buf = UncacheableBuffer(flush=captured.extend, flush_seconds=3600)
    buf.flush()
    assert captured == []


# --- repository -----------------------------------------------------------


def test_record_upserts_hits_and_last_ttl(temp_config_db: None) -> None:
    repo.record_uncacheable([UncacheableAgg("10.0.0.1", "fast.example", "A", "zero-ttl", 0, 2)])
    repo.record_uncacheable([UncacheableAgg("10.0.0.1", "fast.example", "A", "zero-ttl", 5, 3)])
    rows = repo.list_uncacheable()
    assert len(rows) == 1
    assert rows[0]["client"] == "10.0.0.1"
    assert rows[0]["domain"] == "fast.example"
    assert rows[0]["reason"] == "zero-ttl"
    assert rows[0]["hits"] == 5
    assert rows[0]["last_ttl"] == 5


def test_record_keys_on_reason(temp_config_db: None) -> None:
    repo.record_uncacheable([UncacheableAgg("10.0.0.1", "a.example", "A", "nxdomain", None, 1)])
    repo.record_uncacheable([UncacheableAgg("10.0.0.1", "a.example", "A", "nodata", None, 1)])
    rows = repo.list_uncacheable()
    assert {row["reason"] for row in rows} == {"nxdomain", "nodata"}


def test_record_keys_on_client(temp_config_db: None) -> None:
    repo.record_uncacheable([UncacheableAgg("10.0.0.1", "a.example", "A", "nxdomain", None, 3)])
    repo.record_uncacheable([UncacheableAgg("10.0.0.2", "a.example", "A", "nxdomain", None, 5)])
    rows = repo.list_uncacheable()
    assert {row["client"]: row["hits"] for row in rows} == {"10.0.0.1": 3, "10.0.0.2": 5}


def test_record_empty_is_noop(temp_config_db: None) -> None:
    repo.record_uncacheable([])
    assert repo.count_uncacheable() == 0


def test_list_orders_by_hits_desc(temp_config_db: None) -> None:
    now = datetime.now()
    _insert("low.example", "A", "nxdomain", 1, now)
    _insert("high.example", "A", "nxdomain", 9, now)
    rows = repo.list_uncacheable()
    assert [row["domain"] for row in rows] == ["high.example", "low.example"]


def test_list_filters_by_reason(temp_config_db: None) -> None:
    now = datetime.now()
    _insert("a.example", "A", "nxdomain", 3, now)
    _insert("b.example", "A", "nodata", 2, now)
    rows = repo.list_uncacheable(reason="nodata")
    assert [row["domain"] for row in rows] == ["b.example"]
    assert repo.count_uncacheable(reason="nodata") == 1


def test_list_filters_by_client(temp_config_db: None) -> None:
    now = datetime.now()
    _insert("a.example", "A", "nxdomain", 3, now, client="10.0.0.1")
    _insert("b.example", "A", "nxdomain", 2, now, client="10.0.0.2")
    rows = repo.list_uncacheable(client="10.0.0.2")
    assert [row["domain"] for row in rows] == ["b.example"]
    assert repo.count_uncacheable(client="10.0.0.2") == 1


def test_record_prunes_to_cap(temp_config_db: None, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(repo, "MAX_UNCACHEABLE", 2)
    now = datetime.now()
    _insert("a.example", "A", "nxdomain", 1, now - timedelta(hours=3))
    _insert("b.example", "A", "nxdomain", 1, now - timedelta(hours=2))
    _insert("c.example", "A", "nxdomain", 1, now - timedelta(hours=1))
    # Inserting a newer row triggers the prune to the 2 most recent by last_seen.
    repo.record_uncacheable([UncacheableAgg("10.0.0.1", "d.example", "A", "nxdomain", None, 1)])
    assert repo.count_uncacheable() == 2
    assert {row["domain"] for row in repo.list_uncacheable()} == {"d.example", "c.example"}


# --- route ----------------------------------------------------------------


def test_uncacheable_route_envelope(temp_config_db: None) -> None:
    repo.record_uncacheable([UncacheableAgg("10.0.0.5", "nope.example", "A", "nxdomain", None, 4)])
    app = TestApp(create_app())
    resp = app.get("/cache/uncacheable")
    assert resp.status_code == 200
    assert resp.json["status"] == "ok"
    assert resp.json["total"] == 1
    assert resp.json["uncacheable"][0]["client"] == "10.0.0.5"
    assert resp.json["uncacheable"][0]["domain"] == "nope.example"
    assert resp.json["uncacheable"][0]["reason"] == "nxdomain"
    assert resp.json["uncacheable"][0]["hits"] == 4


def test_uncacheable_route_filters_by_reason(temp_config_db: None) -> None:
    repo.record_uncacheable(
        [
            UncacheableAgg("10.0.0.1", "a.example", "A", "nxdomain", None, 1),
            UncacheableAgg("10.0.0.1", "b.example", "A", "nodata", None, 1),
        ]
    )
    app = TestApp(create_app())
    resp = app.get("/cache/uncacheable", params={"reason": "nodata"})
    assert resp.status_code == 200
    assert resp.json["total"] == 1
    assert resp.json["uncacheable"][0]["domain"] == "b.example"


def test_uncacheable_route_filters_by_client(temp_config_db: None) -> None:
    repo.record_uncacheable(
        [
            UncacheableAgg("10.0.0.1", "a.example", "A", "nxdomain", None, 1),
            UncacheableAgg("10.0.0.2", "b.example", "A", "nxdomain", None, 1),
        ]
    )
    app = TestApp(create_app())
    resp = app.get("/cache/uncacheable", params={"client": "10.0.0.2"})
    assert resp.status_code == 200
    assert resp.json["total"] == 1
    assert resp.json["uncacheable"][0]["domain"] == "b.example"
