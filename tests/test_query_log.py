from collections.abc import Iterator
from datetime import datetime, timedelta

import pytest
from sqlalchemy import event
from webtest import TestApp

from mirenai import config, db
from mirenai.db import session_scope
from mirenai.domain.querybuffer import QueryAgg
from mirenai.repository import query_log as repo
from mirenai.repository.models import ClientRequest, QueryLog


@pytest.fixture()
def temp_config_db(tmp_path, monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    for _name in ("config", "stats", "cache", "querylog"):
        monkeypatch.setattr(
            config.settings, f"{_name}_database_url", f"sqlite:///{tmp_path / f'{_name}.db'}"
        )
    db.create_all_schemas()
    yield


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


def test_search_matches_domain(temp_config_db: None) -> None:
    _add_query("10.0.0.1", "ads.example.com")
    _add_query("10.0.0.2", "good.example.org")
    rows = repo.list_queries(search="ads")
    assert [r["domain"] for r in rows] == ["ads.example.com"]
    assert repo.count_queries(search="ads") == 1


def test_search_matches_client(temp_config_db: None) -> None:
    _add_query("10.0.0.1", "a.example")
    _add_query("192.168.1.5", "b.example")
    rows = repo.list_queries(search="192.168")
    assert [r["client"] for r in rows] == ["192.168.1.5"]
    assert repo.count_queries(search="192.168") == 1


def test_client_filter_matches_exact_ip(temp_config_db: None) -> None:
    _add_query("10.0.0.5", "a.example")
    _add_query("10.0.0.50", "b.example")
    rows = repo.list_queries(client="10.0.0.5")
    assert [r["domain"] for r in rows] == ["a.example"]
    assert repo.count_queries(client="10.0.0.5") == 1


def test_client_filter_combines_with_search(temp_config_db: None) -> None:
    _add_query("10.0.0.5", "ads.example.com")
    _add_query("10.0.0.5", "good.example.org")
    _add_query("10.0.0.6", "ads.example.net")
    rows = repo.list_queries(client="10.0.0.5", search="ads")
    assert [r["domain"] for r in rows] == ["ads.example.com"]
    assert repo.count_queries(client="10.0.0.5", search="ads") == 1


def test_search_is_case_insensitive(temp_config_db: None) -> None:
    _add_query("10.0.0.1", "ADS.Example.com")
    assert len(repo.list_queries(search="ads")) == 1


def test_search_escapes_like_wildcards(temp_config_db: None) -> None:
    _add_query("10.0.0.1", "a_b.example")
    _add_query("10.0.0.2", "axb.example")
    # '_' must match literally, not as a LIKE single-char wildcard.
    rows = repo.list_queries(search="a_b")
    assert [r["domain"] for r in rows] == ["a_b.example"]


def test_no_search_returns_all(temp_config_db: None) -> None:
    _add_query("10.0.0.1", "a.example")
    _add_query("10.0.0.2", "b.example")
    assert repo.count_queries() == 2
    assert len(repo.list_queries()) == 2


def test_record_queries_stores_last_response(temp_config_db: None) -> None:
    repo.record_queries(
        [QueryAgg("10.0.0.1", "aria.microsoft.com", "A", 1, "forward", "201.23.89.2")]
    )
    rows = repo.list_queries(search="aria")
    assert rows[0]["last_response"] == "201.23.89.2"


def test_record_queries_refreshes_last_response_on_conflict(temp_config_db: None) -> None:
    repo.record_queries([QueryAgg("10.0.0.1", "aria.microsoft.com", "A", 1, "forward", "1.1.1.1")])
    repo.record_queries([QueryAgg("10.0.0.1", "aria.microsoft.com", "A", 1, "forward", "201.23.89.2")])
    rows = repo.list_queries(search="aria")
    assert rows[0]["count"] == 2
    assert rows[0]["last_response"] == "201.23.89.2"


@pytest.mark.parametrize("pagination", ["", "&limit=1&offset=1"])
def test_queries_retained_first_seen_survives_reset(
    temp_config_db: None, client: TestApp, pagination: str
) -> None:
    db.BlocklistBase.metadata.create_all(db.get_blocklist_engine())
    db.HostsBase.metadata.create_all(db.get_hosts_engine())
    old = datetime.now() - timedelta(days=30)
    newer = old + timedelta(days=10)
    with session_scope() as session:
        session.add_all([
            ClientRequest(
                client=ip, domain="shared.example", qtype=qtype,
                hits=1, first_seen=seen, last_seen=seen,
            )
            for ip, qtype, seen in [
                ("10.0.0.5", "A", old),
                ("10.0.0.5", "AAAA", newer),
                ("10.0.0.9", "A", newer),
            ]
        ])
    _add_query("10.0.0.5", "shared.example")
    assert client.delete("/queries").json["deleted"] == 1
    repo.record_queries([
        QueryAgg("10.0.0.5", "shared.example", qtype, 7, "forward", "1.1.1.1")
        for qtype in ("A", "AAAA")
    ])
    _add_query("10.0.0.9", "shared.example")

    response = client.get(f"/queries?client=10.0.0.5&search=shared{pagination}").json
    assert response["status"] == "ok"
    assert response["total"] == 2
    assert len(response["queries"]) == (1 if pagination else 2)
    for row in response["queries"]:
        assert row["domain_first_seen"] == old.isoformat()
        assert datetime.fromisoformat(row["first_seen"]) > newer
        assert row["count"] == 7
        assert row["last_action"] == "forward"
        assert row["last_response"] == "1.1.1.1"
        assert row["blocked"] is False
    other = client.get("/queries?client=10.0.0.9").json["queries"]
    assert other[0]["domain_first_seen"] == newer.isoformat()
    feed = client.get("/stats/new-domains/recent").json["domains"]
    assert {
        row["client"]: row["first_seen"] for row in feed
    } == {"10.0.0.5": old.isoformat(), "10.0.0.9": newer.isoformat()}


def test_queries_missing_retained_history_is_null_until_flush(temp_config_db: None) -> None:
    _add_query("10.0.0.5", "pending.example")
    row = repo.list_queries()[0]
    assert row["domain_first_seen"] is None
    seen = datetime.now() - timedelta(days=30)
    with session_scope() as session:
        session.add(ClientRequest(
            client="10.0.0.5", domain="pending.example", qtype="AAAA",
            hits=1, first_seen=seen, last_seen=seen,
        ))
    refreshed = repo.list_queries()[0]
    assert refreshed["domain_first_seen"] == seen.isoformat()
    assert refreshed["first_seen"] == row["first_seen"]
    assert repo.list_queries(client="10.0.0.9") == []


def test_queries_retained_history_lookups_are_batched(temp_config_db: None) -> None:
    seen = datetime.now() - timedelta(days=30)
    with session_scope() as session:
        session.add_all([
            QueryLog(
                client="10.0.0.5", domain=f"d{i}.example", qtype="A",
                count=1, first_seen=seen, last_seen=seen,
            )
            for i in range(501)
        ])
        session.add_all([
            ClientRequest(
                client="10.0.0.5", domain=f"d{i}.example", qtype="A",
                hits=1, first_seen=seen, last_seen=seen,
            )
            for i in range(501)
        ])
    lookups: list[int] = []

    def capture(conn, cursor, statement, parameters, context, executemany):
        if statement.lstrip().upper().startswith("SELECT"):
            lookups.append(len(parameters))

    engine = db.get_stats_engine()
    event.listen(engine, "before_cursor_execute", capture)
    try:
        rows = repo.list_queries()
        assert len(rows) == 501
        assert all(row["domain_first_seen"] == seen.isoformat() for row in rows)
        assert len(lookups) == 2
        assert max(lookups) <= 900
        lookups.clear()
        page = repo.list_queries(limit=1, offset=500)
        assert len(page) == 1
        assert page[0]["domain_first_seen"] == seen.isoformat()
        assert lookups == [2]
    finally:
        event.remove(engine, "before_cursor_execute", capture)
