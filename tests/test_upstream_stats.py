from collections.abc import Iterator
from datetime import datetime

import pytest
from webtest import TestApp

from mirenai import config, db
from mirenai.api.app import create_app
from mirenai.db import Base
from mirenai.domain.upstreamstats import UpstreamRttAgg, UpstreamRttBuffer
from mirenai.repository import upstream_stats as repo


@pytest.fixture()
def temp_config_db(tmp_path, monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    db_path = tmp_path / "mirenai.db"
    monkeypatch.setattr(config.settings, "database_url", f"sqlite:///{db_path}")
    # Force the cached engine/session factory to rebuild against the temp DB.
    monkeypatch.setattr(db, "_engine", None)
    monkeypatch.setattr(db, "_session_factory", None)
    Base.metadata.create_all(db.get_engine())
    yield


def _hour(now: datetime) -> datetime:
    return now.replace(minute=0, second=0, microsecond=0)


def test_buffer_aggregates_samples_total_max() -> None:
    captured: list[UpstreamRttAgg] = []
    buf = UpstreamRttBuffer(flush=captured.extend, flush_seconds=3600)
    buf.add("8.8.8.8", 10.0)
    buf.add("8.8.8.8", 30.0)
    buf.add("1.1.1.1", 5.0)
    buf.flush()
    by_addr = {agg.address: agg for agg in captured}
    assert by_addr["8.8.8.8"].samples == 2
    assert by_addr["8.8.8.8"].total_ms == 40.0
    assert by_addr["8.8.8.8"].max_ms == 30.0
    assert by_addr["1.1.1.1"].samples == 1


def test_record_and_list_computes_avg(temp_config_db: None) -> None:
    hour = _hour(datetime.now())
    repo.record_upstream_rtt([UpstreamRttAgg(hour, "8.8.8.8", 2, 40.0, 30.0)])
    rows = repo.list_upstream_rtt()
    assert len(rows) == 1
    assert rows[0]["address"] == "8.8.8.8"
    assert rows[0]["samples"] == 2
    assert rows[0]["avg_ms"] == 20.0
    assert rows[0]["max_ms"] == 30.0


def test_record_upserts_accumulate(temp_config_db: None) -> None:
    hour = _hour(datetime.now())
    repo.record_upstream_rtt([UpstreamRttAgg(hour, "8.8.8.8", 1, 10.0, 10.0)])
    repo.record_upstream_rtt([UpstreamRttAgg(hour, "8.8.8.8", 1, 30.0, 30.0)])
    rows = repo.list_upstream_rtt()
    assert rows[0]["samples"] == 2
    assert rows[0]["avg_ms"] == 20.0
    assert rows[0]["max_ms"] == 30.0  # max is kept, not summed


def test_hours_fill_is_dense_with_nulls(temp_config_db: None) -> None:
    hour = _hour(datetime.now())
    repo.record_upstream_rtt([UpstreamRttAgg(hour, "8.8.8.8", 1, 12.0, 12.0)])
    rows = repo.list_upstream_rtt(hours=3, fill=True)
    assert {r["address"] for r in rows} == {"8.8.8.8"}
    data = [r for r in rows if r["samples"] == 1]
    empty = [r for r in rows if r["samples"] == 0]
    assert len(data) == 1
    assert data[0]["avg_ms"] == 12.0
    assert empty and all(r["avg_ms"] is None and r["max_ms"] is None for r in empty)


def test_route_envelope_and_bad_hours(temp_config_db: None) -> None:
    hour = _hour(datetime.now())
    repo.record_upstream_rtt([UpstreamRttAgg(hour, "8.8.8.8", 2, 40.0, 30.0)])
    app = TestApp(create_app())
    resp = app.get("/stats/upstreams")
    assert resp.status_code == 200
    assert resp.json["status"] == "ok"
    assert resp.json["total"] == 1
    assert resp.json["stats"][0]["avg_ms"] == 20.0
    bad = app.get("/stats/upstreams?hours=abc", status=422)
    assert bad.json["code"] == "validation_error"
