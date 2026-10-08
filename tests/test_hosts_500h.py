from collections.abc import Iterator
from datetime import datetime, timedelta

import pytest
from webtest import TestApp

from mirenai import config, db
from mirenai.api.app import create_app
from mirenai.domain.clientstats import ClientStatAgg
from mirenai.repository import client_stats as stats_repo
from mirenai.repository import hosts as hosts_repo


@pytest.fixture()
def temp_dbs(tmp_path, monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    for name in ("config", "stats", "cache", "querylog"):
        monkeypatch.setattr(
            config.settings, f"{name}_database_url", f"sqlite:///{tmp_path / f'{name}.db'}"
        )
    monkeypatch.setattr(
        config.settings, "localhosts_database_url", f"sqlite:///{tmp_path / 'localhosts.db'}"
    )
    db.create_all_schemas()
    db.HostsBase.metadata.create_all(db.get_hosts_engine())
    yield


def _stat(hour: datetime, client: str, total: int) -> ClientStatAgg:
    return ClientStatAgg(
        hour_start=hour,
        client=client,
        total=total,
        forwarded=0,
        cached=0,
        overridden=0,
        local=0,
        denied=0,
        blocked=0,
        servfail=0,
        foreign=0,
    )


def test_list_hosts_attaches_trailing_window_total(temp_dbs: None) -> None:
    hosts_repo.record_hosts({"10.0.0.5": 7})
    hour = datetime.now().replace(minute=0, second=0, microsecond=0)
    stats_repo.record_client_stats(
        [_stat(hour, "10.0.0.5", 30), _stat(hour - timedelta(hours=2), "10.0.0.5", 12)]
    )
    rows = hosts_repo.list_hosts()
    assert len(rows) == 1
    assert rows[0]["query_count"] == 7  # all-time lifetime counter
    assert rows[0]["500h_queries"] == 42  # summed from the per-hour client stats


def test_host_without_stats_reports_zero(temp_dbs: None) -> None:
    hosts_repo.record_hosts({"10.0.0.9": 3})
    host_id = hosts_repo.list_hosts()[0]["id"]
    row = hosts_repo.get_host(host_id)
    assert row is not None
    assert row["500h_queries"] == 0


def test_get_host_attaches_trailing_window_total(temp_dbs: None) -> None:
    hosts_repo.record_hosts({"10.0.0.5": 1})
    hour = datetime.now().replace(minute=0, second=0, microsecond=0)
    stats_repo.record_client_stats([_stat(hour, "10.0.0.5", 15)])
    host_id = hosts_repo.list_hosts()[0]["id"]
    row = hosts_repo.get_host(host_id)
    assert row is not None
    assert row["500h_queries"] == 15


def test_hosts_route_envelope_carries_field(temp_dbs: None) -> None:
    hosts_repo.record_hosts({"10.0.0.5": 1})
    hour = datetime.now().replace(minute=0, second=0, microsecond=0)
    stats_repo.record_client_stats([_stat(hour, "10.0.0.5", 20)])
    app = TestApp(create_app())
    resp = app.get("/hosts")
    assert resp.status_code == 200
    assert resp.json["hosts"][0]["500h_queries"] == 20
