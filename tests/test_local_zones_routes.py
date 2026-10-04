from collections.abc import Iterator
from datetime import datetime, timedelta

import pytest
from webtest import TestApp

from mirenai import config, db
from mirenai.api.app import create_app
from mirenai.db import Base
from mirenai.domain.localzones import LocalRecord
from mirenai.repository import local_zones as repo
from mirenai.workers import local_zones as worker


@pytest.fixture()
def temp_config_db(tmp_path, monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    db_path = tmp_path / "mirenai.db"
    monkeypatch.setattr(config.settings, "database_url", f"sqlite:///{db_path}")
    monkeypatch.setattr(db, "_engine", None)
    monkeypatch.setattr(db, "_session_factory", None)
    Base.metadata.create_all(db.get_engine())
    yield


def test_list_empty(temp_config_db: None) -> None:
    app = TestApp(create_app())
    resp = app.get("/local-zones")
    assert resp.json == {"status": "ok", "local_zones": [], "total": 0}


def test_create_valid(temp_config_db: None) -> None:
    app = TestApp(create_app())
    resp = app.post_json(
        "/local-zones",
        {"name": "home", "url": "https://raw.githubusercontent.com/me/dns/main/home.txt"},
    )
    assert resp.status_code == 201
    assert resp.json["local_zone"]["name"] == "home"
    assert resp.json["local_zone"]["update_interval_seconds"] == 86400


def test_create_honors_custom_interval(temp_config_db: None) -> None:
    app = TestApp(create_app())
    resp = app.post_json(
        "/local-zones",
        {"name": "home", "url": "https://example.com/a.txt", "update_interval_seconds": 3600},
    )
    assert resp.status_code == 201
    assert resp.json["local_zone"]["update_interval_seconds"] == 3600


def test_create_invalid_url(temp_config_db: None) -> None:
    app = TestApp(create_app())
    resp = app.post_json("/local-zones", {"name": "home", "url": "ftp://nope"}, status=422)
    assert resp.json["code"] == "validation_error"


def test_create_duplicate_name_conflict(temp_config_db: None) -> None:
    app = TestApp(create_app())
    app.post_json("/local-zones", {"name": "home", "url": "https://example.com/a.txt"})
    resp = app.post_json(
        "/local-zones", {"name": "home", "url": "https://example.com/b.txt"}, status=409
    )
    assert resp.json["code"] == "conflict"


def test_get_update_delete_lifecycle(temp_config_db: None) -> None:
    app = TestApp(create_app())
    created = app.post_json("/local-zones", {"name": "home", "url": "https://example.com/a.txt"})
    zone_id = created.json["local_zone"]["id"]
    assert app.get(f"/local-zones/{zone_id}").json["local_zone"]["name"] == "home"
    updated = app.put_json(f"/local-zones/{zone_id}", {"enabled": False})
    assert updated.json["local_zone"]["enabled"] is False
    assert app.delete(f"/local-zones/{zone_id}").json == {"status": "ok", "deleted": zone_id}
    app.get(f"/local-zones/{zone_id}", status=404)


def test_zone_records_listing(temp_config_db: None) -> None:
    app = TestApp(create_app())
    created = repo.create_local_zone({"name": "home", "url": "https://example.com/a.txt"})
    repo.replace_records(
        created["id"],
        [
            LocalRecord("host.example.lan", "A", "192.0.2.10", 300),
            LocalRecord("10.2.0.192.in-addr.arpa", "PTR", "host.example.lan", 300),
        ],
    )
    resp = app.get(f"/local-zones/{created['id']}/records")
    assert resp.json["total"] == 2
    assert {r["type"] for r in resp.json["records"]} == {"A", "PTR"}
    app.get("/local-zones/999/records", status=404)


def test_local_records_merged_view(temp_config_db: None) -> None:
    app = TestApp(create_app())
    zone = repo.create_local_zone({"name": "home", "url": "https://example.com/a.txt"})
    repo.replace_records(
        zone["id"],
        [
            LocalRecord("host.example.lan", "A", "192.0.2.10", 300),
            LocalRecord("www.example.lan", "CNAME", "host.example.lan", 300),
        ],
    )
    all_resp = app.get("/local-records")
    assert all_resp.json["total"] == 2
    assert all_resp.json["records"][0]["zone_name"] == "home"
    typed = app.get("/local-records?type=cname")
    assert typed.json["total"] == 1
    assert typed.json["records"][0]["name"] == "www.example.lan"
    searched = app.get("/local-records?search=host.example.lan")
    assert searched.json["total"] >= 1


def test_refresh_success(temp_config_db: None, monkeypatch: pytest.MonkeyPatch) -> None:
    app = TestApp(create_app())
    zone = repo.create_local_zone({"name": "home", "url": "https://example.com/a.txt"})
    monkeypatch.setattr(worker, "download_text", lambda url: "192.0.2.10,host.example.lan")
    resp = app.post(f"/local-zones/{zone['id']}/refresh")
    assert resp.status_code == 200
    assert resp.json["local_zone"]["record_count"] == 2
    assert repo.count_records(zone["id"]) == 2


def test_refresh_download_failure_returns_502(
    temp_config_db: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    app = TestApp(create_app())
    zone = repo.create_local_zone({"name": "home", "url": "https://example.com/a.txt"})

    def _boom(url: str) -> str:
        raise worker.LocalZoneDownloadError("HTTP 404")

    monkeypatch.setattr(worker, "download_text", _boom)
    resp = app.post(f"/local-zones/{zone['id']}/refresh", status=502)
    assert resp.json["code"] == "download_failed"
    assert resp.json["detail"] == "HTTP 404"


def test_refresh_unknown_zone_404(temp_config_db: None) -> None:
    app = TestApp(create_app())
    app.post("/local-zones/999/refresh", status=404)


def test_is_due_uses_seconds_interval() -> None:
    now = datetime.now()
    recent = {
        "enabled": True,
        "last_downloaded_at": (now - timedelta(seconds=30)).isoformat(),
        "update_interval_seconds": 60,
    }
    overdue = {
        "enabled": True,
        "last_downloaded_at": (now - timedelta(seconds=90)).isoformat(),
        "update_interval_seconds": 60,
    }
    assert worker._is_due(recent, now) is False
    assert worker._is_due(overdue, now) is True


def test_is_due_never_downloaded_is_due() -> None:
    row = {"enabled": True, "last_downloaded_at": None, "update_interval_seconds": 86400}
    assert worker._is_due(row, datetime.now()) is True


def test_is_due_disabled_zone_is_not_due() -> None:
    row = {"enabled": False, "last_downloaded_at": None, "update_interval_seconds": 1}
    assert worker._is_due(row, datetime.now()) is False
