from collections.abc import Iterator
from datetime import datetime

import pytest
from sqlalchemy import select
from webtest import TestApp

from mirenai import config, db
from mirenai.api.app import create_app
from mirenai.db import BlocklistBase, blocklist_session_scope
from mirenai.repository import blocklists as repo
from mirenai.repository.models import BlocklistDomain


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


def _first_seen_map(blocklist_id: int) -> dict[str, datetime]:
    with blocklist_session_scope() as session:
        rows = session.execute(
            select(BlocklistDomain.domain, BlocklistDomain.first_seen).where(
                BlocklistDomain.blocklist_id == blocklist_id
            )
        ).all()
    return {domain: first_seen for domain, first_seen in rows}


# --- replace_domains diff -------------------------------------------------


def test_replace_preserves_first_seen_and_applies_delta(temp_dbs: None) -> None:
    bl = repo.create_blocklist({"name": "a", "url": "https://a.example/h"})
    repo.replace_domains(bl["id"], ["keep.example.com", "drop.example.com"])
    before = _first_seen_map(bl["id"])

    repo.replace_domains(bl["id"], ["keep.example.com", "add.example.com"])
    after = _first_seen_map(bl["id"])

    assert set(after) == {"keep.example.com", "add.example.com"}  # drop removed, add inserted
    assert after["keep.example.com"] == before["keep.example.com"]  # timestamp preserved
    assert after["add.example.com"] >= before["keep.example.com"]  # new entry stamped later


def test_replace_with_empty_clears_all(temp_dbs: None) -> None:
    bl = repo.create_blocklist({"name": "a", "url": "https://a.example/h"})
    repo.replace_domains(bl["id"], ["a.example.com", "b.example.com"])
    repo.replace_domains(bl["id"], [])
    assert repo.count_domains(bl["id"]) == 0


def test_replace_is_idempotent_for_identical_set(temp_dbs: None) -> None:
    bl = repo.create_blocklist({"name": "a", "url": "https://a.example/h"})
    repo.replace_domains(bl["id"], ["a.example.com", "b.example.com"])
    before = _first_seen_map(bl["id"])
    repo.replace_domains(bl["id"], ["b.example.com", "a.example.com"])  # same set, new order
    after = _first_seen_map(bl["id"])
    assert after == before  # nothing re-stamped


# --- newest-entries feed --------------------------------------------------


def test_list_new_domains_newest_first(temp_dbs: None) -> None:
    bl = repo.create_blocklist({"name": "listA", "url": "https://a.example/h"})
    repo.replace_domains(bl["id"], ["old1.example.com", "old2.example.com"])
    repo.replace_domains(bl["id"], ["old1.example.com", "old2.example.com", "new.example.com"])
    rows = repo.list_new_domains()
    assert rows[0]["domain"] == "new.example.com"
    assert rows[0]["blocklist_name"] == "listA"
    assert rows[0]["blocklist_id"] == bl["id"]
    assert "first_seen" in rows[0]


def test_list_new_domains_filters_by_blocklist(temp_dbs: None) -> None:
    first = repo.create_blocklist({"name": "a", "url": "https://a.example/h"})
    second = repo.create_blocklist({"name": "b", "url": "https://b.example/h"})
    repo.replace_domains(first["id"], ["one.example.com"])
    repo.replace_domains(second["id"], ["two.example.com"])
    rows = repo.list_new_domains(blocklist_id=second["id"])
    assert [row["domain"] for row in rows] == ["two.example.com"]
    assert repo.count_new_domains(blocklist_id=second["id"]) == 1
    assert repo.count_new_domains() == 2


# --- route ----------------------------------------------------------------


def test_recent_route_envelope(temp_dbs: None) -> None:
    bl = repo.create_blocklist({"name": "listA", "url": "https://a.example/h"})
    repo.replace_domains(bl["id"], ["a.example.com"])
    app = TestApp(create_app())
    resp = app.get("/blocklists/recent")
    assert resp.status_code == 200
    assert resp.json["status"] == "ok"
    assert resp.json["total"] == 1
    assert resp.json["domains"][0]["domain"] == "a.example.com"
    assert resp.json["domains"][0]["blocklist_name"] == "listA"


def test_recent_route_filters_by_blocklist_id(temp_dbs: None) -> None:
    first = repo.create_blocklist({"name": "a", "url": "https://a.example/h"})
    second = repo.create_blocklist({"name": "b", "url": "https://b.example/h"})
    repo.replace_domains(first["id"], ["one.example.com"])
    repo.replace_domains(second["id"], ["two.example.com"])
    app = TestApp(create_app())
    resp = app.get("/blocklists/recent", params={"blocklist_id": second["id"]})
    assert resp.status_code == 200
    assert resp.json["total"] == 1
    assert resp.json["domains"][0]["domain"] == "two.example.com"
