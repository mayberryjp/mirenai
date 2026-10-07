from collections.abc import Iterator
from datetime import datetime, timedelta

import pytest
from webtest import TestApp

from mirenai import config, db
from mirenai.api.app import create_app
from mirenai.db import session_scope
from mirenai.domain.cacheoutcome import CACHE_OUTCOME_REASONS, CacheOutcomeAgg, CacheOutcomeBuffer
from mirenai.repository import cache_outcome as repo
from mirenai.repository.models import CacheOutcomeHourly


@pytest.fixture()
def temp_config_db(tmp_path, monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    for _name in ("config", "stats", "cache", "querylog"):
        monkeypatch.setattr(
            config.settings, f"{_name}_database_url", f"sqlite:///{tmp_path / f'{_name}.db'}"
        )
    db.create_all_schemas()
    yield


def _insert(hour_start: datetime, reason: str, hits: int) -> None:
    with session_scope() as session:
        session.add(CacheOutcomeHourly(hour_start=hour_start, reason=reason, hits=hits))


def _floor(moment: datetime) -> datetime:
    return moment.replace(minute=0, second=0, microsecond=0)


# --- buffer ---------------------------------------------------------------


def test_buffer_aggregates_by_reason() -> None:
    captured: list[CacheOutcomeAgg] = []
    buf = CacheOutcomeBuffer(flush=captured.extend, flush_seconds=3600)
    buf.add("cached")
    buf.add("cached")
    buf.add("nxdomain")
    buf.flush()
    agg = {row.reason: row.hits for row in captured}
    assert agg == {"cached": 2, "nxdomain": 1}
    # every bucket is floored to the hour
    assert all(row.hour_start.minute == 0 and row.hour_start.second == 0 for row in captured)


def test_buffer_flush_empty_is_noop() -> None:
    captured: list[CacheOutcomeAgg] = []
    buf = CacheOutcomeBuffer(flush=captured.extend, flush_seconds=3600)
    buf.flush()
    assert captured == []


# --- repository -----------------------------------------------------------


def test_record_upserts_hits_on_hour_reason(temp_config_db: None) -> None:
    hour = _floor(datetime.now())
    repo.record_cache_outcomes([CacheOutcomeAgg(hour, "cached", 2)])
    repo.record_cache_outcomes([CacheOutcomeAgg(hour, "cached", 3)])
    rows = repo.list_cache_outcomes()
    assert len(rows) == 1
    assert rows[0]["reason"] == "cached"
    assert rows[0]["hits"] == 5


def test_record_empty_is_noop(temp_config_db: None) -> None:
    repo.record_cache_outcomes([])
    assert repo.count_cache_outcomes() == 0


def test_record_purges_old_buckets(temp_config_db: None, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(repo, "RETENTION_HOURS", 2)
    now = _floor(datetime.now())
    old = now - timedelta(hours=5)
    _insert(old, "cached", 1)
    repo.record_cache_outcomes([CacheOutcomeAgg(now, "cached", 1)])
    hours = {row["hour_start"] for row in repo.list_cache_outcomes()}
    assert old.isoformat() not in hours
    assert now.isoformat() in hours


def test_list_filters_by_hours(temp_config_db: None) -> None:
    now = _floor(datetime.now())
    _insert(now, "cached", 1)
    _insert(now - timedelta(hours=10), "cached", 1)
    rows = repo.list_cache_outcomes(hours=1)
    assert [row["hour_start"] for row in rows] == [now.isoformat()]


def test_fill_produces_dense_series(temp_config_db: None) -> None:
    hour = _floor(datetime.now())
    _insert(hour, "cached", 5)
    _insert(hour, "nxdomain", 2)
    rows = repo.list_cache_outcomes(hours=1, fill=True)
    by_key = {(row["hour_start"], row["reason"]): row["hits"] for row in rows}
    assert by_key[(hour.isoformat(), "cached")] == 5
    assert by_key[(hour.isoformat(), "nxdomain")] == 2
    assert by_key[(hour.isoformat(), "nodata")] == 0  # zero-filled
    reasons_for_hour = {row["reason"] for row in rows if row["hour_start"] == hour.isoformat()}
    assert reasons_for_hour == set(CACHE_OUTCOME_REASONS)


# --- route ----------------------------------------------------------------


def test_cache_outcomes_route_envelope(temp_config_db: None) -> None:
    _insert(_floor(datetime.now()), "nxdomain", 7)
    app = TestApp(create_app())
    resp = app.get("/stats/cache-outcomes")
    assert resp.status_code == 200
    assert resp.json["status"] == "ok"
    assert resp.json["total"] == 1
    assert resp.json["stats"][0]["reason"] == "nxdomain"
    assert resp.json["stats"][0]["hits"] == 7


def test_cache_outcomes_route_fill(temp_config_db: None) -> None:
    hour = _floor(datetime.now())
    _insert(hour, "cached", 3)
    app = TestApp(create_app())
    resp = app.get("/stats/cache-outcomes", params={"hours": 1})
    assert resp.status_code == 200
    rows = resp.json["stats"]
    found = [r for r in rows if r["hour_start"] == hour.isoformat() and r["reason"] == "cached"]
    assert found and found[0]["hits"] == 3
    # dense: the current hour carries a row for every outcome bucket
    reasons = {r["reason"] for r in rows if r["hour_start"] == hour.isoformat()}
    assert reasons == set(CACHE_OUTCOME_REASONS)
