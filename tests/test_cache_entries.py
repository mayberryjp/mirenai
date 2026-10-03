from collections.abc import Iterator
from datetime import datetime, timedelta

import pytest
from dnslib import QTYPE, RR, A, DNSRecord
from webtest import TestApp

from mirenai import config, db
from mirenai.api.app import create_app
from mirenai.db import Base
from mirenai.domain.cache import TTLCache
from mirenai.domain.cacheview import CacheEntrySnapshot, build_cache_snapshot
from mirenai.repository import cache_entries as repo


@pytest.fixture()
def temp_config_db(tmp_path, monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    db_path = tmp_path / "mirenai.db"
    monkeypatch.setattr(config.settings, "database_url", f"sqlite:///{db_path}")
    # Force the cached engine/session factory to rebuild against the temp DB.
    monkeypatch.setattr(db, "_engine", None)
    monkeypatch.setattr(db, "_session_factory", None)
    Base.metadata.create_all(db.get_engine())
    yield


def _packed_reply(name: str, ip: str, ttl: int = 300) -> bytes:
    reply = DNSRecord.question(name, "A").reply()
    reply.add_answer(RR(name, QTYPE.A, ttl=ttl, rdata=A(ip)))
    return reply.pack()


def _snapshot(domain: str, response: str, *, ttl: int = 300, expires_in: int = 300) -> CacheEntrySnapshot:
    return CacheEntrySnapshot(
        domain=domain,
        qtype="A",
        qclass="IN",
        response=response,
        answers=1,
        ttl=ttl,
        expires_at=datetime.now() + timedelta(seconds=expires_in),
    )


def test_build_cache_snapshot_decodes_entry() -> None:
    cache: TTLCache[bytes] = TTLCache(10)
    # Key format mirrors the resolver: name|qtype|qclass with numeric wire values.
    cache.set("aria.microsoft.com|1|1", _packed_reply("aria.microsoft.com", "201.23.89.2"), 300)
    rows = build_cache_snapshot(cache)
    assert len(rows) == 1
    row = rows[0]
    assert row.domain == "aria.microsoft.com"
    assert row.qtype == "A"
    assert row.qclass == "IN"
    assert row.response == "201.23.89.2"
    assert row.answers == 1
    assert row.ttl == 300
    assert row.expires_at > datetime.now()


def test_build_cache_snapshot_skips_expired() -> None:
    cache: TTLCache[bytes] = TTLCache(10)
    cache.set("gone.example|1|1", _packed_reply("gone.example", "1.2.3.4"), 0)
    assert build_cache_snapshot(cache) == []


def test_record_and_list_cache_entries(temp_config_db: None) -> None:
    repo.record_cache_entries([_snapshot("aria.microsoft.com", "201.23.89.2", expires_in=120)])
    rows = repo.list_cache_entries()
    assert len(rows) == 1
    assert rows[0]["domain"] == "aria.microsoft.com"
    assert rows[0]["response"] == "201.23.89.2"
    assert rows[0]["qtype"] == "A"
    assert 0 < rows[0]["remaining_ttl"] <= 120


def test_record_cache_entries_replaces_previous_snapshot(temp_config_db: None) -> None:
    repo.record_cache_entries([_snapshot("first.example", "1.1.1.1")])
    repo.record_cache_entries([_snapshot("second.example", "2.2.2.2")])
    rows = repo.list_cache_entries()
    assert [row["domain"] for row in rows] == ["second.example"]


def test_record_empty_snapshot_clears_table(temp_config_db: None) -> None:
    repo.record_cache_entries([_snapshot("first.example", "1.1.1.1")])
    repo.record_cache_entries([])
    assert repo.list_cache_entries() == []
    assert repo.count_cache_entries() == 0
    assert repo.cache_updated_at() is None


def test_search_filters_by_domain_or_response(temp_config_db: None) -> None:
    repo.record_cache_entries(
        [
            _snapshot("ads.example.com", "10.0.0.9"),
            _snapshot("good.example.org", "201.23.89.2"),
        ]
    )
    assert [row["domain"] for row in repo.list_cache_entries(search="ads")] == ["ads.example.com"]
    assert [row["domain"] for row in repo.list_cache_entries(search="201.23")] == ["good.example.org"]
    assert repo.count_cache_entries(search="ads") == 1


def test_cache_route_lists_entries(temp_config_db: None) -> None:
    repo.record_cache_entries([_snapshot("aria.microsoft.com", "201.23.89.2")])
    app = TestApp(create_app())
    body = app.get("/cache").json
    assert body["status"] == "ok"
    assert body["total"] == 1
    assert body["entries"][0]["domain"] == "aria.microsoft.com"
    assert body["entries"][0]["response"] == "201.23.89.2"
    assert body["updated_at"] is not None
