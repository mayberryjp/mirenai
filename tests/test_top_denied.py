from collections.abc import Iterator
from datetime import datetime, timedelta

import pytest
from dnslib import DNSRecord
from sqlalchemy import select
from webtest import TestApp

from mirenai import config, db
from mirenai.db import session_scope
from mirenai.domain.policy import PolicyRule
from mirenai.domain.querybuffer import QueryAgg, QueryBuffer
from mirenai.domain.state import RuntimeSettings
from mirenai.repository import hosts
from mirenai.repository import query_log as repo
from mirenai.repository.models import PolicyDeniedQuery, QueryLog
from test_resolver import _make_resolver


@pytest.fixture()
def temp_dbs(tmp_path, monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    for name in ("config", "stats", "cache", "querylog"):
        monkeypatch.setattr(
            config.settings, f"{name}_database_url", f"sqlite:///{tmp_path / f'{name}.db'}"
        )
    monkeypatch.setattr(
        config.settings, "localhosts_database_url", f"sqlite:///{tmp_path / 'hosts.db'}"
    )
    db.create_all_schemas()
    db.HostsBase.metadata.create_all(db.get_hosts_engine())
    yield


def _add(
    client: str, domain: str, count: int, qtype: str = "A",
    first_seen: datetime | None = None, last_seen: datetime | None = None,
) -> None:
    now = datetime.now()
    with session_scope() as session:
        session.add(
            PolicyDeniedQuery(
                client=client, domain=domain, qtype=qtype, count=count,
                first_seen=first_seen or now, last_seen=last_seen or now,
            )
        )


def _seed() -> None:
    _add(
        "10.0.0.1", "denied.example", 7,
        first_seen=datetime(2026, 11, 1, 8), last_seen=datetime(2026, 11, 1, 10),
    )
    _add(
        "10.0.0.1", "denied.example", 3, "AAAA",
        first_seen=datetime(2026, 11, 1, 9), last_seen=datetime(2026, 11, 1, 11),
    )
    _add(
        "10.0.0.2", "denied.example", 5,
        first_seen=datetime(2026, 11, 1, 9), last_seen=datetime(2026, 11, 1, 10),
    )
    _add("10.0.0.2", "another.example", 12)
    buffer = QueryBuffer(repo.record_queries, 5)
    for action, domain in (("forward", "allowed.example"), ("blocklist", "blocked.example")):
        for _ in range(30):
            buffer.add("10.0.0.1", domain, "A", action, "")
    buffer.flush()


def test_top_denied_ranks_actual_denials_and_breaks_down_clients(
    temp_dbs: None, client: TestApp,
) -> None:
    _seed()
    body = client.get("/queries/top-denied").json
    assert body["status"] == "ok"
    assert body["total"] == 2
    assert [row["domain"] for row in body["domains"]] == ["denied.example", "another.example"]
    first = body["domains"][0]
    assert first["count"] == 15
    assert first["clients"] == [
        {"client": "10.0.0.1", "count": 10}, {"client": "10.0.0.2", "count": 5},
    ]
    assert first["first_seen"] == "2026-11-01T08:00:00"
    assert first["last_seen"] == "2026-11-01T11:00:00"
    assert set(first) == {"domain", "count", "first_seen", "last_seen", "clients"}


def test_top_denied_defaults_to_twenty_and_honors_limit(temp_dbs: None, client: TestApp) -> None:
    for index in range(25):
        _add("10.0.0.1", f"d{index:02}.example", index + 1)
    body = client.get("/queries/top-denied").json
    assert body["total"] == 20
    assert body["domains"][0]["domain"] == "d24.example"
    body = client.get("/queries/top-denied?limit=10").json
    assert body["total"] == 10
    assert len(body["domains"]) == 10
    body = client.get("/queries/top-denied?limit=100").json
    assert body["total"] == 25


@pytest.mark.parametrize("limit", ["abc", "0", "-1", "1.5"])
def test_top_denied_rejects_invalid_limit(
    temp_dbs: None, client: TestApp, limit: str,
) -> None:
    response = client.get("/queries/top-denied", {"limit": limit}, status=422)
    assert response.json["code"] == "validation_error"


def test_top_denied_client_filter_is_exact_and_applied_before_ranking(
    temp_dbs: None, client: TestApp,
) -> None:
    _seed()
    _add("10.0.0.10", "other-client.example", 100)
    body = client.get("/queries/top-denied?client=10.0.0.1&limit=1").json
    assert body["total"] == 1
    assert body["domains"][0]["count"] == 10
    assert body["domains"][0]["clients"] == [{"client": "10.0.0.1", "count": 10}]
    assert client.get("/queries/top-denied?client=10.0.0.99").json["total"] == 0


def test_top_denied_ties_are_deterministic(temp_dbs: None, client: TestApp) -> None:
    _add("10.0.0.2", "b.example", 5)
    _add("10.0.0.1", "b.example", 5)
    _add("10.0.0.3", "a.example", 10)
    body = client.get("/queries/top-denied").json
    assert [row["domain"] for row in body["domains"]] == ["a.example", "b.example"]
    assert body["domains"][1]["clients"] == [
        {"client": "10.0.0.1", "count": 5}, {"client": "10.0.0.2", "count": 5},
    ]


def test_top_denied_empty_without_denials(temp_dbs: None, client: TestApp) -> None:
    assert client.get("/queries/top-denied").json == {"status": "ok", "domains": [], "total": 0}


def test_mixed_action_batch_tracks_only_denies_and_preserves_latest_query(temp_dbs: None) -> None:
    buffer = QueryBuffer(repo.record_queries, 5)
    for action in ("forward", "deny", "blocklist", "deny", "forward"):
        buffer.add("10.0.0.1", "mixed.example", "A", action, "")
    buffer.flush()
    queries = repo.list_queries()
    assert queries[0]["count"] == 5
    assert queries[0]["last_action"] == "forward"
    assert repo.aggregate_domain_totals(denied_only=True)[0]["count"] == 2
    buffer.add("10.0.0.1", "mixed.example", "A", "deny", "")
    buffer.add("10.0.0.1", "mixed.example", "A", "override", "192.0.2.1")
    buffer.flush()
    assert repo.aggregate_domain_totals(denied_only=True)[0]["count"] == 3
    assert repo.list_queries()[0]["count"] == 7
    assert repo.list_queries()[0]["last_action"] == "override"


def test_denial_timestamps_ignore_later_allows_and_out_of_order_flushes(temp_dbs: None) -> None:
    first = datetime(2026, 11, 1, 8)
    last = first + timedelta(hours=1)
    for timestamp in (last, first):
        repo.record_queries([
            QueryAgg(
                "10.0.0.1", "denied.example", "A", 2, "forward", "192.0.2.1",
                denied_count=1, denied_first_seen=timestamp, denied_last_seen=timestamp,
            )
        ])
    repo.record_queries([QueryAgg("10.0.0.1", "denied.example", "A", 1, "forward", "192.0.2.1")])
    row = repo.aggregate_domain_totals(denied_only=True)[0]
    assert row["first_seen"] == first.isoformat()
    assert row["last_seen"] == last.isoformat()
    assert row["count"] == 2


def test_query_reset_also_resets_denials(temp_dbs: None, client: TestApp) -> None:
    buffer = QueryBuffer(repo.record_queries, 5)
    buffer.add("10.0.0.1", "denied.example", "A", "deny", "")
    buffer.flush()
    assert client.delete("/queries").json == {"status": "ok", "deleted": 1}
    assert client.get("/queries/top-denied").json["total"] == 0


def test_host_delete_removes_only_its_denials(temp_dbs: None) -> None:
    _add("10.0.0.1", "denied.example", 5)
    _add("10.0.0.2", "denied.example", 3)
    hosts.record_hosts({"10.0.0.1": 1})
    assert hosts.delete_host(hosts.list_hosts()[0]["id"]) is True
    assert repo.aggregate_domain_totals(denied_only=True)[0]["count"] == 3


def test_existing_query_log_is_not_used_as_approximate_denial_history(temp_dbs: None) -> None:
    with session_scope() as session:
        session.add(QueryLog(client="10.0.0.1", domain="old.example", qtype="A",
                             count=100, last_action="deny"))
    assert repo.aggregate_domain_totals(denied_only=True) == []
    PolicyDeniedQuery.__table__.drop(db.get_querylog_engine())
    db.reset_engines()
    db.create_all_schemas()
    buffer = QueryBuffer(repo.record_queries, 5)
    buffer.add("10.0.0.1", "old.example", "A", "deny", "")
    buffer.flush()
    assert repo.aggregate_domain_totals(denied_only=True)[0]["count"] == 1
    assert repo.list_queries()[0]["count"] == 101


def test_denials_persist_across_restart(temp_dbs: None) -> None:
    _add("10.0.0.1", "denied.example", 5)
    db.reset_engines()
    db.create_all_schemas()
    assert repo.aggregate_domain_totals(denied_only=True)[0]["count"] == 5


@pytest.mark.parametrize("policy,settings", [
    (PolicyRule("*", "*", "deny"), RuntimeSettings()),
    (None, RuntimeSettings(default_action="deny")),
])
def test_resolver_explicit_and_default_denies_are_counted(
    temp_dbs: None, client: TestApp, policy: PolicyRule | None, settings: RuntimeSettings,
) -> None:
    buffer = QueryBuffer(repo.record_queries, 5)
    resolver = _make_resolver([policy] if policy else [], settings, buffer=buffer)
    resolver.handle(DNSRecord.question("EXAMPLE.COM.", "A"), "10.0.0.1")
    buffer.flush()
    body = client.get("/queries/top-denied").json
    assert body["domains"][0]["domain"] == "example.com"
    assert body["domains"][0]["count"] == 1


def test_resolver_blocklist_nodata_and_foreign_do_not_count_as_policy_denies(temp_dbs: None) -> None:
    buffer = QueryBuffer(repo.record_queries, 5)
    resolver = _make_resolver(
        [], RuntimeSettings(ipv6_enabled=False),
        blocklist=frozenset({"example.com"}), buffer=buffer,
    )
    resolver.handle(DNSRecord.question("example.com", "A"), "10.0.0.1")
    resolver.handle(DNSRecord.question("example.com", "AAAA"), "10.0.0.1")
    resolver.record_foreign("203.0.113.1")
    buffer.flush()
    assert repo.aggregate_domain_totals(denied_only=True) == []


@pytest.mark.parametrize("filtered", [False, True])
def test_denied_client_breakdown_handles_chunking(temp_dbs: None, filtered: bool) -> None:
    domains = [f"d{i}.example" for i in range(501)]
    with session_scope() as session:
        session.add_all([
            PolicyDeniedQuery(client=client, domain=domain, qtype="A", count=1)
            for domain in domains for client in ("10.0.0.1", "10.0.0.2")
        ])
    result = repo.clients_for_domains(
        domains, denied_only=True, client="10.0.0.1" if filtered else None,
    )
    assert len(result) == 501
    assert all(len(clients) == (1 if filtered else 2) for clients in result.values())


def test_invalid_denial_aggregate_rolls_back_the_batch(temp_dbs: None) -> None:
    with pytest.raises(ValueError, match="timestamps"):
        repo.record_queries([QueryAgg("10.0.0.1", "example.com", "A", 1, "deny", "", denied_count=1)])
    with session_scope() as session:
        assert session.scalars(select(QueryLog)).all() == []
        assert session.scalars(select(PolicyDeniedQuery)).all() == []
