from collections.abc import Iterator

import pytest
from webtest import TestApp

from mirenai import config, db
from mirenai.api.app import create_app
from mirenai.db import BlocklistBase
from mirenai.repository import blocklists as repo


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


def _seed_two_lists() -> tuple[int, int]:
    first = repo.create_blocklist({"name": "HaGeZi Multi PRO", "url": "https://a.example/hosts"})
    second = repo.create_blocklist({"name": "StevenBlack", "url": "https://b.example/hosts"})
    repo.replace_domains(first["id"], ["ads.example.com", "tracker.example.net"])
    repo.replace_domains(second["id"], ["ads.example.com", "good.example.org"])
    return first["id"], second["id"]


def test_search_finds_partial_match_and_maps_names(temp_dbs: None) -> None:
    first_id, second_id = _seed_two_lists()
    matches = repo.search_domains("ads.example")
    assert matches == [
        {"domain": "ads.example.com", "blocklist_id": first_id, "blocklist_name": "HaGeZi Multi PRO"},
        {"domain": "ads.example.com", "blocklist_id": second_id, "blocklist_name": "StevenBlack"},
    ]


def test_search_matches_bare_substring(temp_dbs: None) -> None:
    _seed_two_lists()
    domains = {match["domain"] for match in repo.search_domains("example")}
    assert domains == {"ads.example.com", "tracker.example.net", "good.example.org"}


def test_search_is_case_insensitive(temp_dbs: None) -> None:
    _seed_two_lists()
    assert repo.search_domains("TRACKER")[0]["domain"] == "tracker.example.net"


def test_search_escapes_like_wildcards(temp_dbs: None) -> None:
    _seed_two_lists()
    # "%" and "_" must match literally, not as LIKE wildcards.
    assert repo.search_domains("%") == []
    assert repo.search_domains("ads_example") == []


def test_search_pagination_and_count(temp_dbs: None) -> None:
    _seed_two_lists()
    assert repo.count_domain_matches("ads.example") == 2
    page = repo.search_domains("ads.example", limit=1, offset=0)
    assert len(page) == 1


def test_search_no_match_returns_empty(temp_dbs: None) -> None:
    _seed_two_lists()
    assert repo.search_domains("nonexistent.invalid") == []
    assert repo.count_domain_matches("nonexistent.invalid") == 0


def test_search_route_returns_matches(temp_dbs: None) -> None:
    _seed_two_lists()
    app = TestApp(create_app())
    body = app.get("/blocklists/search", {"q": "tracker"}).json
    assert body["status"] == "ok"
    assert body["total"] == 1
    assert body["matches"][0] == {
        "domain": "tracker.example.net",
        "blocklist_id": 1,
        "blocklist_name": "HaGeZi Multi PRO",
    }


def test_search_route_requires_q(temp_dbs: None) -> None:
    app = TestApp(create_app())
    body = app.get("/blocklists/search", status=422).json
    assert body["status"] == "error"
    assert body["code"] == "validation_error"
