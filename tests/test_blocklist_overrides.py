from collections.abc import Iterator

import pytest
from webtest import TestApp

from mirenai import config, db
from mirenai.api.app import create_app
from mirenai.db import BlocklistBase
from mirenai.repository import blocklist_overrides as override_repo
from mirenai.repository import blocklists as repo
from mirenai.workers import blocklist_downloader as downloader


@pytest.fixture()
def temp_dbs(tmp_path, monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    for _name in ("config", "stats", "cache", "querylog"):
        monkeypatch.setattr(
            config.settings, f"{_name}_database_url", f"sqlite:///{tmp_path / f'{_name}.db'}"
        )
    monkeypatch.setattr(
        config.settings, "blocklist_database_url", f"sqlite:///{tmp_path / 'blocklist.db'}"
    )
    db.create_all_schemas()
    BlocklistBase.metadata.create_all(db.get_blocklist_engine())
    yield


# --- repository ---


def test_create_and_list_overrides(temp_dbs: None) -> None:
    created = override_repo.create_override("aria.microsoft.com")
    assert created["domain"] == "aria.microsoft.com"
    assert created["id"] > 0
    assert [row["domain"] for row in override_repo.list_overrides()] == ["aria.microsoft.com"]
    assert override_repo.count_overrides() == 1


def test_list_overrides_ordered_by_domain(temp_dbs: None) -> None:
    override_repo.create_override("b.example.com")
    override_repo.create_override("a.example.com")
    assert [row["domain"] for row in override_repo.list_overrides()] == [
        "a.example.com",
        "b.example.com",
    ]


def test_delete_override(temp_dbs: None) -> None:
    created = override_repo.create_override("aria.microsoft.com")
    assert override_repo.delete_override(created["id"]) is True
    assert override_repo.list_overrides() == []
    assert override_repo.delete_override(created["id"]) is False


def test_load_override_domains(temp_dbs: None) -> None:
    override_repo.create_override("a.example.com")
    override_repo.create_override("b.example.com")
    assert override_repo.load_override_domains() == frozenset({"a.example.com", "b.example.com"})


# --- routes ---


def test_post_override_creates_and_normalizes(temp_dbs: None) -> None:
    app = TestApp(create_app())
    resp = app.post_json("/blocklists/overrides", {"domain": "Aria.Microsoft.COM."})
    assert resp.status_code == 201
    assert resp.json["status"] == "ok"
    assert resp.json["override"]["domain"] == "aria.microsoft.com"


def test_post_override_rejects_invalid_domain(temp_dbs: None) -> None:
    app = TestApp(create_app())
    resp = app.post_json("/blocklists/overrides", {"domain": "not a domain"}, status=422)
    assert resp.json["code"] == "validation_error"


def test_post_override_rejects_unknown_field(temp_dbs: None) -> None:
    app = TestApp(create_app())
    resp = app.post_json("/blocklists/overrides", {"domain": "a.example.com", "x": 1}, status=422)
    assert resp.json["code"] == "validation_error"


def test_post_override_duplicate_conflicts(temp_dbs: None) -> None:
    app = TestApp(create_app())
    app.post_json("/blocklists/overrides", {"domain": "a.example.com"})
    resp = app.post_json("/blocklists/overrides", {"domain": "a.example.com"}, status=409)
    assert resp.json["code"] == "conflict"


def test_get_overrides_lists(temp_dbs: None) -> None:
    override_repo.create_override("a.example.com")
    app = TestApp(create_app())
    resp = app.get("/blocklists/overrides")
    assert resp.status_code == 200
    assert resp.json["total"] == 1
    assert resp.json["overrides"][0]["domain"] == "a.example.com"


def test_delete_override_route(temp_dbs: None) -> None:
    created = override_repo.create_override("a.example.com")
    app = TestApp(create_app())
    resp = app.delete(f"/blocklists/overrides/{created['id']}")
    assert resp.status_code == 200
    assert resp.json == {"status": "ok", "deleted": created["id"]}


def test_delete_override_route_not_found(temp_dbs: None) -> None:
    app = TestApp(create_app())
    resp = app.delete("/blocklists/overrides/99999", status=404)
    assert resp.json["code"] == "not_found"


def test_overrides_path_not_shadowed_by_blocklist_id(temp_dbs: None) -> None:
    # "/blocklists/overrides" must route to overrides, not "/blocklists/<id:int>".
    repo.create_blocklist({"name": "L", "url": "https://l.example/hosts"})
    override_repo.create_override("a.example.com")
    app = TestApp(create_app())
    resp = app.get("/blocklists/overrides")
    assert resp.status_code == 200
    assert resp.json["overrides"][0]["domain"] == "a.example.com"


# --- download integration ---


def test_refresh_blocklist_strips_override_domains(
    temp_dbs: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    created = repo.create_blocklist({"name": "L", "url": "https://l.example/hosts"})
    override_repo.create_override("aria.microsoft.com")
    monkeypatch.setattr(
        downloader,
        "download_text",
        lambda url: "0.0.0.0 aria.microsoft.com\n0.0.0.0 ads.example.com\n",
    )
    downloader.refresh_blocklist(created["id"])
    assert repo.list_domains(created["id"]) == ["ads.example.com"]
    row = repo.get_blocklist(created["id"])
    assert row is not None
    assert row["domain_count"] == 1
