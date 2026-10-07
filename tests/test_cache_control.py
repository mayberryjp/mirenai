from collections.abc import Iterator

import pytest
from webtest import TestApp

from mirenai import config, db
from mirenai.api.app import create_app
from mirenai.repository import cache_control as repo


@pytest.fixture()
def temp_config_db(tmp_path, monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    for _name in ("config", "stats", "cache", "querylog"):
        monkeypatch.setattr(
            config.settings, f"{_name}_database_url", f"sqlite:///{tmp_path / f'{_name}.db'}"
        )
    db.create_all_schemas()
    yield


def test_get_flush_request_absent_by_default(temp_config_db: None) -> None:
    assert repo.get_cache_flush_request() is None


def test_request_and_get_cache_flush(temp_config_db: None) -> None:
    requested_at = repo.request_cache_flush()
    assert repo.get_cache_flush_request() == requested_at


def test_request_cache_flush_upserts_single_row(temp_config_db: None) -> None:
    first = repo.request_cache_flush()
    second = repo.request_cache_flush()
    assert second >= first  # ISO strings sort chronologically
    assert repo.get_cache_flush_request() == second


def test_flush_cache_route(temp_config_db: None) -> None:
    app = TestApp(create_app())
    resp = app.post("/cache/flush")
    assert resp.status_code == 200
    assert resp.json["status"] == "ok"
    assert resp.json["requested_at"] == repo.get_cache_flush_request()
