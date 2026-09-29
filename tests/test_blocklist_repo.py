from collections.abc import Iterator

import pytest

from mirenai import config, db
from mirenai.db import Base
from mirenai.repository import blocklists as repo


@pytest.fixture()
def temp_config_db(tmp_path, monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    db_path = tmp_path / "mirenai.db"
    monkeypatch.setattr(config.settings, "database_url", f"sqlite:///{db_path}")
    # Force the cached engine/session factory to rebuild against the temp DB.
    monkeypatch.setattr(db, "_engine", None)
    monkeypatch.setattr(db, "_session_factory", None)
    Base.metadata.create_all(db.get_engine())
    yield


def test_new_blocklist_has_no_format(temp_config_db: None) -> None:
    row = repo.create_blocklist({"name": "test", "url": "https://example.com/hosts"})
    assert row["format"] is None


def test_record_success_persists_format(temp_config_db: None) -> None:
    created = repo.create_blocklist({"name": "test", "url": "https://example.com/hosts"})
    repo.record_success(created["id"], 3, "hosts")
    row = repo.get_blocklist(created["id"])
    assert row is not None
    assert row["format"] == "hosts"
    assert row["domain_count"] == 3


def test_ensure_default_blocklist_seeds_disabled_when_empty(temp_config_db: None) -> None:
    repo.ensure_default_blocklist()
    rows = repo.list_blocklists()
    assert len(rows) == 1
    assert rows[0]["url"] == (
        "https://raw.githubusercontent.com/hagezi/dns-blocklists/main/adblock/pro.txt"
    )
    assert rows[0]["enabled"] is False


def test_ensure_default_blocklist_is_idempotent(temp_config_db: None) -> None:
    repo.ensure_default_blocklist()
    repo.ensure_default_blocklist()
    assert len(repo.list_blocklists()) == 1


def test_ensure_default_blocklist_skips_when_any_exists(temp_config_db: None) -> None:
    repo.create_blocklist({"name": "mine", "url": "https://example.com/hosts"})
    repo.ensure_default_blocklist()
    rows = repo.list_blocklists()
    assert len(rows) == 1
    assert rows[0]["name"] == "mine"

