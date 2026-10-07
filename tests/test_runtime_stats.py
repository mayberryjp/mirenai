from collections.abc import Iterator

import pytest
from webtest import TestApp

from mirenai import config, db
from mirenai.api.app import create_app
from mirenai.repository import runtime_stats as repo


@pytest.fixture()
def temp_config_db(tmp_path, monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    for _name in ("config", "stats", "cache", "querylog"):
        monkeypatch.setattr(
            config.settings, f"{_name}_database_url", f"sqlite:///{tmp_path / f'{_name}.db'}"
        )
    db.create_all_schemas()
    yield


def test_record_and_load_runtime_stats(temp_config_db: None) -> None:
    repo.record_runtime_stats({"cache_size": 5, "blocklist_domains": 100})
    snapshot = repo.load_runtime_stats()
    assert snapshot["stats"] == {"cache_size": 5, "blocklist_domains": 100}
    assert snapshot["updated_at"] is not None


def test_record_runtime_stats_upserts(temp_config_db: None) -> None:
    repo.record_runtime_stats({"cache_size": 5})
    repo.record_runtime_stats({"cache_size": 9})
    assert repo.load_runtime_stats()["stats"]["cache_size"] == 9


def test_load_runtime_stats_empty(temp_config_db: None) -> None:
    snapshot = repo.load_runtime_stats()
    assert snapshot["stats"] == {}
    assert snapshot["updated_at"] is None


def test_record_runtime_stats_ignores_empty(temp_config_db: None) -> None:
    repo.record_runtime_stats({})
    assert repo.load_runtime_stats()["stats"] == {}


def test_runtime_stats_route_envelope(monkeypatch: pytest.MonkeyPatch) -> None:
    def _fake() -> dict[str, object]:
        return {
            "stats": {"cache_size": 7, "cache_capacity": 10000},
            "updated_at": "2026-09-28T00:00:00",
        }

    monkeypatch.setattr(repo, "load_runtime_stats", _fake)
    app = TestApp(create_app())

    resp = app.get("/stats/runtime")
    assert resp.status_code == 200
    assert resp.json["status"] == "ok"
    assert resp.json["stats"]["cache_size"] == 7
    assert resp.json["stats"]["cache_capacity"] == 10000
    assert resp.json["updated_at"] == "2026-09-28T00:00:00"
