from collections.abc import Iterator
from datetime import datetime, timedelta

import pytest
from webtest import TestApp

from mirenai import config, db
from mirenai.api.app import create_app
from mirenai.db import session_scope
from mirenai.repository import blocklist_size as repo
from mirenai.repository import blocklists as bl_repo
from mirenai.repository.models import BlocklistSizeHourly


@pytest.fixture()
def temp_dbs(tmp_path, monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    for _name in ("config", "stats", "cache", "querylog"):
        monkeypatch.setattr(
            config.settings, f"{_name}_database_url", f"sqlite:///{tmp_path / f'{_name}.db'}"
        )
    db.create_all_schemas()
    yield


def _insert(hour_start: datetime, domains: int) -> None:
    with session_scope() as session:
        session.add(BlocklistSizeHourly(hour_start=hour_start, domains=domains))


# --- gauge repository -----------------------------------------------------


def test_record_upserts_current_hour_as_gauge(temp_dbs: None) -> None:
    repo.record_blocklist_size(100)
    repo.record_blocklist_size(150)  # same hour -> overwrite (gauge), not add
    rows = repo.list_blocklist_size()
    assert len(rows) == 1
    assert rows[0]["domains"] == 150


def test_record_prunes_beyond_retention(temp_dbs: None, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(repo, "RETENTION_HOURS", 3)
    aligned = datetime.now().replace(minute=0, second=0, microsecond=0)
    _insert(aligned - timedelta(hours=5), 10)  # older than retention
    _insert(aligned - timedelta(hours=1), 20)
    repo.record_blocklist_size(30)  # current hour; triggers the prune
    assert {row["domains"] for row in repo.list_blocklist_size()} == {20, 30}


def test_list_orders_newest_first_and_filters_hours(temp_dbs: None) -> None:
    aligned = datetime.now().replace(minute=0, second=0, microsecond=0)
    _insert(aligned - timedelta(hours=10), 1)
    _insert(aligned - timedelta(hours=2), 2)
    _insert(aligned - timedelta(hours=1), 3)
    assert [row["domains"] for row in repo.list_blocklist_size()] == [3, 2, 1]
    # hours=3 keeps the -1h and -2h buckets; the -10h bucket falls outside.
    assert [row["domains"] for row in repo.list_blocklist_size(hours=3)] == [3, 2]
    assert repo.count_blocklist_size(hours=3) == 2


def test_list_paginates(temp_dbs: None) -> None:
    aligned = datetime.now().replace(minute=0, second=0, microsecond=0)
    _insert(aligned - timedelta(hours=3), 1)
    _insert(aligned - timedelta(hours=2), 2)
    _insert(aligned - timedelta(hours=1), 3)
    page = repo.list_blocklist_size(limit=2, offset=0)
    assert [row["domains"] for row in page] == [3, 2]


# --- enabled-size source --------------------------------------------------


def test_total_enabled_domain_count_sums_enabled_only(temp_dbs: None) -> None:
    enabled = bl_repo.create_blocklist({"name": "a", "url": "https://a.example/h"})
    disabled = bl_repo.create_blocklist(
        {"name": "b", "url": "https://b.example/h", "enabled": False}
    )
    bl_repo.record_success(enabled["id"], 100, "hosts")
    bl_repo.record_success(disabled["id"], 50, "hosts")
    assert bl_repo.total_enabled_domain_count() == 100


def test_total_enabled_domain_count_zero_when_none(temp_dbs: None) -> None:
    assert bl_repo.total_enabled_domain_count() == 0


# --- route ----------------------------------------------------------------


def test_size_history_route_envelope(temp_dbs: None) -> None:
    repo.record_blocklist_size(123)
    app = TestApp(create_app())
    resp = app.get("/blocklists/size-history")
    assert resp.status_code == 200
    assert resp.json["status"] == "ok"
    assert resp.json["total"] == 1
    assert resp.json["size_history"][0]["domains"] == 123


def test_size_history_route_hours_filter(temp_dbs: None) -> None:
    aligned = datetime.now().replace(minute=0, second=0, microsecond=0)
    _insert(aligned - timedelta(hours=10), 1)
    _insert(aligned - timedelta(hours=1), 2)
    app = TestApp(create_app())
    resp = app.get("/blocklists/size-history", params={"hours": 3})
    assert resp.status_code == 200
    assert resp.json["total"] == 1
    assert resp.json["size_history"][0]["domains"] == 2
