from collections.abc import Iterator
from datetime import datetime

import pytest

from mirenai import config, db
from mirenai.db import Base
from mirenai.domain.clientstats import ClientStatAgg
from mirenai.repository import client_stats as repo


@pytest.fixture()
def temp_config_db(tmp_path, monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    db_path = tmp_path / "mirenai.db"
    monkeypatch.setattr(config.settings, "database_url", f"sqlite:///{db_path}")
    monkeypatch.setattr(db, "_engine", None)
    monkeypatch.setattr(db, "_session_factory", None)
    Base.metadata.create_all(db.get_engine())
    yield


def _agg(hour: datetime, client: str, *, total: int, foreign: int) -> ClientStatAgg:
    return ClientStatAgg(
        hour_start=hour,
        client=client,
        total=total,
        forwarded=0,
        cached=0,
        overridden=0,
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
