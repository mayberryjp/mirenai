from collections.abc import Iterator
from datetime import datetime

import pytest
from webtest import TestApp

from mirenai import config, db
from mirenai.api.app import create_app
from mirenai.db import Base, BlocklistBase, session_scope
from mirenai.repository import blocklists as repo
from mirenai.repository.models import ClientRequest, QueryLog


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


def _seed_list(domains: list[str], *, enabled: bool = True, name: str = "L") -> int:
    row = repo.create_blocklist(
        {"name": name, "url": f"https://{name}.example/hosts", "enabled": enabled}
    )
    repo.replace_domains(row["id"], domains)
    return int(row["id"])


def _add_query(client: str, domain: str, qtype: str = "A") -> None:
    now = datetime.now()
    with session_scope() as session:
        session.add(
            QueryLog(
                client=client,
                domain=domain,
                qtype=qtype,
                count=1,
                last_action="forward",
                first_seen=now,
                last_seen=now,
            )
        )


def _add_request(client: str, domain: str, qtype: str = "A") -> None:
    now = datetime.now()
    with session_scope() as session:
        session.add(
            ClientRequest(
                client=client,
                domain=domain,
                qtype=qtype,
                hits=1,
                first_seen=now,
                last_seen=now,
            )
        )


def test_find_blocked_domains_exact_match_enabled_only(temp_dbs: None) -> None:
    _seed_list(["ads.example.com"], enabled=True, name="on")
    _seed_list(["tracker.example.net"], enabled=False, name="off")
    matched = repo.find_blocked_domains(["ads.example.com", "tracker.example.net", "safe.org"])
    # Only the enabled list's domain matches; the disabled list is ignored.
    assert matched == frozenset({"ads.example.com"})


def test_find_blocked_domains_empty_candidates(temp_dbs: None) -> None:
    _seed_list(["ads.example.com"])
    assert repo.find_blocked_domains([]) == frozenset()
    assert repo.find_blocked_domains(["", ""]) == frozenset()


def test_find_blocked_domains_no_enabled_lists(temp_dbs: None) -> None:
    _seed_list(["ads.example.com"], enabled=False)
    assert repo.find_blocked_domains(["ads.example.com"]) == frozenset()


def test_annotate_blocked_flags_exact_and_subdomain(temp_dbs: None) -> None:
    _seed_list(["example.com"])  # covers example.com and any subdomain
    rows = [
        {"domain": "example.com"},
        {"domain": "ads.example.com"},
        {"domain": "safe.org"},
    ]
    repo.annotate_blocked(rows)
    assert [row["blocked"] for row in rows] == [True, True, False]


def test_annotate_blocked_empty_rows(temp_dbs: None) -> None:
    assert repo.annotate_blocked([]) == []


def test_queries_route_includes_blocked(temp_dbs: None) -> None:
    _seed_list(["ads.example.com"])
    _add_query("10.0.0.1", "ads.example.com")
    _add_query("10.0.0.2", "safe.org")
    app = TestApp(create_app())
    body = app.get("/queries").json
    flags = {row["domain"]: row["blocked"] for row in body["queries"]}
    assert flags == {"ads.example.com": True, "safe.org": False}


def test_recent_new_domains_route_includes_blocked(temp_dbs: None) -> None:
    _seed_list(["example.com"])
    _add_request("10.0.0.1", "ads.example.com")  # subdomain of a listed name
    _add_request("10.0.0.1", "safe.org")
    app = TestApp(create_app())
    body = app.get("/stats/new-domains/recent").json
    flags = {row["domain"]: row["blocked"] for row in body["domains"]}
    assert flags == {"ads.example.com": True, "safe.org": False}
