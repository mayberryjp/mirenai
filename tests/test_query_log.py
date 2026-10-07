from collections.abc import Iterator
from datetime import datetime

import pytest

from mirenai import config, db
from mirenai.db import session_scope
from mirenai.domain.querybuffer import QueryAgg
from mirenai.repository import query_log as repo
from mirenai.repository.models import QueryLog


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
