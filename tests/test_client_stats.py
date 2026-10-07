from collections.abc import Iterator
from datetime import datetime

import pytest

from mirenai import config, db
from mirenai.domain.clientstats import ClientStatAgg, ClientStatsBuffer
from mirenai.repository import client_stats as repo


@pytest.fixture()
def temp_config_db(tmp_path, monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    for _name in ("config", "stats", "cache", "querylog"):
        monkeypatch.setattr(
            config.settings, f"{_name}_database_url", f"sqlite:///{tmp_path / f'{_name}.db'}"
        )
    db.create_all_schemas()
    yield


def _agg(
    hour: datetime, client: str, *, total: int, foreign: int = 0, local: int = 0
) -> ClientStatAgg:
    return ClientStatAgg(
        hour_start=hour,
        client=client,
        total=total,
        forwarded=0,
        cached=0,
        overridden=0,
        local=local,
        denied=0,
        blocked=0,
        servfail=0,
        foreign=foreign,
    )


def test_foreign_column_persists_and_upserts(temp_config_db: None) -> None:
    hour = datetime.now().replace(minute=0, second=0, microsecond=0)
    repo.record_client_stats([_agg(hour, "foreign", total=2, foreign=2)])
    repo.record_client_stats([_agg(hour, "foreign", total=3, foreign=3)])
    rows = repo.list_client_stats(client="foreign")
    assert len(rows) == 1
    assert rows[0]["foreign"] == 5
    assert rows[0]["total"] == 5


def test_foreign_counts_in_site_totals(temp_config_db: None) -> None:
    hour = datetime.now().replace(minute=0, second=0, microsecond=0)
    repo.record_client_stats(
        [
            _agg(hour, "foreign", total=4, foreign=4),
            _agg(hour, "10.0.0.1", total=1, foreign=0),
        ]
    )
    site = repo.list_site_hourly_stats()
    assert len(site) == 1
    assert site[0]["foreign"] == 4


def test_local_column_persists_and_upserts(temp_config_db: None) -> None:
    hour = datetime.now().replace(minute=0, second=0, microsecond=0)
    repo.record_client_stats([_agg(hour, "10.0.0.1", total=2, local=2)])
    repo.record_client_stats([_agg(hour, "10.0.0.1", total=3, local=3)])
    rows = repo.list_client_stats(client="10.0.0.1")
    assert len(rows) == 1
    assert rows[0]["local"] == 5
    assert rows[0]["total"] == 5


def test_local_counts_in_site_totals(temp_config_db: None) -> None:
    hour = datetime.now().replace(minute=0, second=0, microsecond=0)
    repo.record_client_stats(
        [
            _agg(hour, "10.0.0.1", total=4, local=4),
            _agg(hour, "10.0.0.2", total=1, local=0),
        ]
    )
    site = repo.list_site_hourly_stats()
    assert len(site) == 1
    assert site[0]["local"] == 4


def test_local_result_maps_to_local_column_not_overridden() -> None:
    captured: list[ClientStatAgg] = []
    buffer = ClientStatsBuffer(flush=captured.extend, flush_seconds=3600)
    buffer.add("10.0.0.1", "local")
    buffer.add("10.0.0.1", "override")
    buffer.flush()
    assert len(captured) == 1
    assert captured[0].local == 1
    assert captured[0].overridden == 1
