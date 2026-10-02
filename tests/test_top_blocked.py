from collections.abc import Iterator
from datetime import datetime

import pytest
from webtest import TestApp

from mirenai import config, db
from mirenai.api.app import create_app
from mirenai.db import Base, BlocklistBase, session_scope
from mirenai.repository import blocklists as blocklists_repo
from mirenai.repository import query_log as repo
from mirenai.repository.models import QueryLog


@pytest.fixture()
def temp_dbs(tmp_path, monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    config_path = tmp_path / "mirenai.db"
    blocklist_path = tmp_path / "blocklist.db"
    monkeypatch.setattr(config.settings, "database_url", f"sqlite:///{config_path}")
    monkeypatch.setattr(config.settings, "blocklist_database_url", f"sqlite:///{blocklist_path}")
    # Force the cached engines/session factories to rebuild against the temp DBs.
    monkeypatch.setattr(db, "_engine", None)
    monkeypatch.setattr(db, "_session_factory", None)
    monkeypatch.setattr(db, "_blocklist_engine", None)
    monkeypatch.setattr(db, "_blocklist_session_factory", None)
    Base.metadata.create_all(db.get_engine())
    BlocklistBase.metadata.create_all(db.get_blocklist_engine())
    yield


def _add_query(
    client: str,
    domain: str,
    count: int,
    *,
    qtype: str = "A",
    first_seen: datetime | None = None,
    last_seen: datetime | None = None,
) -> None:
    now = datetime.now()
    with session_scope() as session:
        session.add(
            QueryLog(
                client=client,
                domain=domain,
                qtype=qtype,
                count=count,
                last_action="blocklist",
                first_seen=first_seen or now,
                last_seen=last_seen or now,
            )
        )


def _seed() -> dict[str, int]:
    """Two enabled lists, one disabled list, plus a spread of query-log rows."""
    hagezi = blocklists_repo.create_blocklist({"name": "HaGeZi", "url": "https://a.example/h"})
    steven = blocklists_repo.create_blocklist({"name": "StevenBlack", "url": "https://b.example/h"})
    disabled = blocklists_repo.create_blocklist(
        {"name": "Disabled", "url": "https://c.example/h", "enabled": False}
    )
    blocklists_repo.replace_domains(hagezi["id"], ["ads.example.com", "tracker.example.net"])
    blocklists_repo.replace_domains(steven["id"], ["ads.example.com"])
    blocklists_repo.replace_domains(disabled["id"], ["secret.example.io"])

    # ads.example.com — two clients, blocked (highest total among blocked).
    _add_query(
        "10.0.0.1",
        "ads.example.com",
        10,
        first_seen=datetime(2026, 1, 1, 8, 0, 0),
        last_seen=datetime(2026, 1, 2, 9, 0, 0),
    )
    _add_query(
        "10.0.0.2",
        "ads.example.com",
        5,
        first_seen=datetime(2026, 1, 3, 8, 0, 0),
        last_seen=datetime(2026, 1, 5, 9, 0, 0),
    )
    # sub.tracker.example.net — blocked via the parent suffix tracker.example.net.
    _add_query("10.0.0.1", "sub.tracker.example.net", 7)
    # good.example.org — highest raw count, but on no blocklist: must be excluded.
    _add_query("10.0.0.3", "good.example.org", 100)
    # secret.example.io — only on a DISABLED list: must be excluded.
    _add_query("10.0.0.1", "secret.example.io", 50)
    return {"hagezi": hagezi["id"], "steven": steven["id"], "disabled": disabled["id"]}


def test_aggregate_domain_totals_sums_and_orders(temp_dbs: None) -> None:
    _seed()
    totals = repo.aggregate_domain_totals()
    by_domain = {row["domain"]: row for row in totals}
    # good.example.org (100) is highest overall; aggregation does not filter here.
    assert [row["domain"] for row in totals][0] == "good.example.org"
    ads = by_domain["ads.example.com"]
    assert ads["count"] == 15
    assert ads["first_seen"] == datetime(2026, 1, 1, 8, 0, 0).isoformat()
    assert ads["last_seen"] == datetime(2026, 1, 5, 9, 0, 0).isoformat()


def test_clients_for_domains_breaks_down_per_client(temp_dbs: None) -> None:
    _seed()
    clients = repo.clients_for_domains(["ads.example.com"])
    assert clients["ads.example.com"] == [
        {"client": "10.0.0.1", "count": 10},
        {"client": "10.0.0.2", "count": 5},
    ]


def test_clients_for_domains_empty_input(temp_dbs: None) -> None:
    assert repo.clients_for_domains([]) == {}


def test_find_domain_sources_matches_suffix_and_skips_disabled(temp_dbs: None) -> None:
    ids = _seed()
    sources = blocklists_repo.find_domain_sources(
        ["sub.tracker.example.net", "ads.example.com", "good.example.org", "secret.example.io"]
    )
    # Suffix match: the queried subdomain is sourced from the listed parent domain.
    assert sources["sub.tracker.example.net"] == [
        {
            "blocklist_id": ids["hagezi"],
            "blocklist_name": "HaGeZi",
            "matched_domain": "tracker.example.net",
        }
    ]
    # ads.example.com is on both enabled lists.
    ads_names = {s["blocklist_name"] for s in sources["ads.example.com"]}
    assert ads_names == {"HaGeZi", "StevenBlack"}
    # Not blocked / only on a disabled list → absent.
    assert "good.example.org" not in sources
    assert "secret.example.io" not in sources


def test_find_domain_sources_no_enabled_lists(temp_dbs: None) -> None:
    assert blocklists_repo.find_domain_sources(["ads.example.com"]) == {}


def test_top_blocked_route_ranks_blocked_domains(temp_dbs: None) -> None:
    ids = _seed()
    app = TestApp(create_app())
    body = app.get("/queries/top-blocked").json

    assert body["status"] == "ok"
    assert body["total"] == 2
    # Ordered by summed count desc; non-blocked good.example.org (100) is excluded.
    assert [row["domain"] for row in body["domains"]] == [
        "ads.example.com",
        "sub.tracker.example.net",
    ]

    ads = body["domains"][0]
    assert ads["count"] == 15
    assert ads["first_seen"] == datetime(2026, 1, 1, 8, 0, 0).isoformat()
    assert ads["last_seen"] == datetime(2026, 1, 5, 9, 0, 0).isoformat()
    assert ads["clients"] == [
        {"client": "10.0.0.1", "count": 10},
        {"client": "10.0.0.2", "count": 5},
    ]
    assert {s["blocklist_name"] for s in ads["blocklists"]} == {"HaGeZi", "StevenBlack"}

    tracker = body["domains"][1]
    assert tracker["blocklists"] == [
        {
            "blocklist_id": ids["hagezi"],
            "blocklist_name": "HaGeZi",
            "matched_domain": "tracker.example.net",
        }
    ]


def test_top_blocked_route_honours_limit(temp_dbs: None) -> None:
    _seed()
    app = TestApp(create_app())
    body = app.get("/queries/top-blocked", {"limit": 1}).json
    assert body["total"] == 1
    assert [row["domain"] for row in body["domains"]] == ["ads.example.com"]


def test_top_blocked_route_rejects_bad_limit(temp_dbs: None) -> None:
    _seed()
    app = TestApp(create_app())
    body = app.get("/queries/top-blocked", {"limit": "abc"}, status=422).json
    assert body["status"] == "error"
    assert body["code"] == "validation_error"


def test_top_blocked_route_empty_when_no_blocklists(temp_dbs: None) -> None:
    _add_query("10.0.0.1", "ads.example.com", 3)
    app = TestApp(create_app())
    body = app.get("/queries/top-blocked").json
    assert body == {"status": "ok", "domains": [], "total": 0}
