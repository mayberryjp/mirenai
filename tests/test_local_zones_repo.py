from collections.abc import Iterator

import pytest
from sqlalchemy.exc import IntegrityError

from mirenai import config, db
from mirenai.domain.localzones import LocalRecord
from mirenai.repository import local_zones as repo


@pytest.fixture()
def temp_config_db(tmp_path, monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    for _name in ("config", "stats", "cache", "querylog"):
        monkeypatch.setattr(
            config.settings, f"{_name}_database_url", f"sqlite:///{tmp_path / f'{_name}.db'}"
        )
    db.create_all_schemas()
    yield


def test_create_list_get_count(temp_config_db: None) -> None:
    created = repo.create_local_zone({"name": "home", "url": "https://example.com/zone.txt"})
    assert created["update_interval_seconds"] == 86400
    assert created["enabled"] is True
    assert created["record_count"] == 0
    assert repo.count_local_zones() == 1
    assert repo.get_local_zone(created["id"])["name"] == "home"
    assert repo.list_local_zones()[0]["name"] == "home"


def test_duplicate_name_conflict(temp_config_db: None) -> None:
    repo.create_local_zone({"name": "dup", "url": "https://example.com/a.txt"})
    with pytest.raises(IntegrityError):
        repo.create_local_zone({"name": "dup", "url": "https://example.com/b.txt"})


def test_update_local_zone(temp_config_db: None) -> None:
    row = repo.create_local_zone({"name": "home", "url": "https://example.com/zone.txt"})
    updated = repo.update_local_zone(row["id"], {"enabled": False, "update_interval_seconds": 600})
    assert updated["enabled"] is False
    assert updated["update_interval_seconds"] == 600
    assert repo.update_local_zone(999, {"enabled": False}) is None


def test_replace_records_and_list(temp_config_db: None) -> None:
    row = repo.create_local_zone({"name": "home", "url": "https://example.com/zone.txt"})
    repo.replace_records(
        row["id"],
        [
            LocalRecord("host.example.lan", "A", "192.0.2.10", 300),
            LocalRecord("10.2.0.192.in-addr.arpa", "PTR", "host.example.lan", 300),
        ],
    )
    assert repo.count_records(row["id"]) == 2
    records = repo.list_records(row["id"])
    assert {r["type"] for r in records} == {"A", "PTR"}
    # Replacing swaps the whole set.
    repo.replace_records(row["id"], [LocalRecord("only.example.lan", "A", "192.0.2.99", 300)])
    assert repo.count_records(row["id"]) == 1


def test_delete_zone_removes_records(temp_config_db: None) -> None:
    row = repo.create_local_zone({"name": "home", "url": "https://example.com/zone.txt"})
    repo.replace_records(row["id"], [LocalRecord("host.example.lan", "A", "192.0.2.10", 300)])
    assert repo.delete_local_zone(row["id"]) is True
    assert repo.count_records(row["id"]) == 0
    assert repo.delete_local_zone(row["id"]) is False


def test_load_local_records_only_enabled(temp_config_db: None) -> None:
    on = repo.create_local_zone({"name": "on", "url": "https://example.com/a.txt"})
    off = repo.create_local_zone(
        {"name": "off", "url": "https://example.com/b.txt", "enabled": False}
    )
    repo.replace_records(on["id"], [LocalRecord("a.example.lan", "A", "192.0.2.1", 300)])
    repo.replace_records(off["id"], [LocalRecord("b.example.lan", "A", "192.0.2.2", 300)])
    loaded = repo.load_local_records()
    assert [r.name for r in loaded] == ["a.example.lan"]


def test_list_all_records_search_and_type(temp_config_db: None) -> None:
    zone = repo.create_local_zone({"name": "home", "url": "https://example.com/zone.txt"})
    repo.replace_records(
        zone["id"],
        [
            LocalRecord("host.example.lan", "A", "192.0.2.10", 300),
            LocalRecord("10.2.0.192.in-addr.arpa", "PTR", "host.example.lan", 300),
            LocalRecord("www.example.lan", "CNAME", "host.example.lan", 300),
        ],
    )
    all_rows = repo.list_all_records()
    assert all(row["zone_name"] == "home" for row in all_rows)
    assert repo.count_all_records() == 3
    assert repo.count_all_records(rtype="a") == 1
    assert repo.count_all_records(rtype="PTR") == 1
    searched = repo.list_all_records(search="www")
    assert len(searched) == 1
    assert searched[0]["name"] == "www.example.lan"


def test_record_success_and_failure(temp_config_db: None) -> None:
    row = repo.create_local_zone({"name": "home", "url": "https://example.com/zone.txt"})
    repo.record_success(row["id"], 7)
    updated = repo.get_local_zone(row["id"])
    assert updated["record_count"] == 7
    assert updated["last_status"] == "ok: 7 records"
    assert updated["last_downloaded_at"] is not None
    repo.record_failure(row["id"], "HTTP 404")
    assert repo.get_local_zone(row["id"])["last_status"] == "error: HTTP 404"
