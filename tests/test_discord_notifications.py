from collections.abc import Iterator
from datetime import datetime, timedelta
import threading
from unittest.mock import Mock
from urllib.error import HTTPError, URLError

import pytest
from dnslib import QTYPE, DNSRecord
from sqlalchemy import select
from webtest import TestApp

from mirenai import config, db
from mirenai.db import session_scope
from mirenai.domain.notifications import DomainObservation, NotificationBuffer
from mirenai.domain.policy import PolicyRule
from mirenai.domain.state import RuntimeSettings
from mirenai.integrations import discord
from mirenai.repository import hosts, notifications, settings
from mirenai.repository.models import AppSetting, ClientRequest, DomainNotification
from mirenai.workers.notifications import NotificationWorker
from test_resolver import _make_resolver

WEBHOOK = "https://discord.com/api/webhooks/123456/test_token"


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


def _enable() -> None:
    settings.update_settings({
        "discord_webhook_url": WEBHOOK, "discord_notifications_enabled": True,
    })


def _event(
    client: str = "10.0.0.1", domain: str = "example.com", qtype: str = "A",
    action: str = "forward", rcode: str = "NOERROR", enabled: bool = True,
    query_type_enabled: bool = True,
) -> DomainObservation:
    return DomainObservation(
        client, domain, qtype, action, rcode, datetime.now(), enabled, query_type_enabled
    )


def _rows() -> list[DomainNotification]:
    with session_scope() as session:
        return list(session.scalars(select(DomainNotification).order_by(DomainNotification.id)))


def test_settings_api_defaults_and_partial_updates(temp_dbs: None, client: TestApp) -> None:
    values = client.get("/settings").json["settings"]
    assert values["discord_notifications_enabled"] is False
    assert values["discord_webhook_url"] == ""
    assert values["discord_query_types"] == {name: True for name in QTYPE.reverse}
    response = client.put_json("/settings", {"discord_webhook_url": f" {WEBHOOK} "})
    assert response.json["settings"]["discord_webhook_url"] == WEBHOOK
    response = client.put_json("/settings", {"discord_notifications_enabled": True})
    assert response.json["status"] == "ok"
    assert response.json["settings"]["discord_notifications_enabled"] is True
    client.put_json("/settings", {"cache_enabled": False})
    assert settings.load_runtime_settings().discord_webhook_url == WEBHOOK
    client.put_json("/settings", {"discord_webhook_url": ""}, status=422)
    assert settings.load_runtime_settings().discord_webhook_url == WEBHOOK
    response = client.put_json("/settings", {
        "discord_notifications_enabled": False, "discord_webhook_url": "",
    })
    assert response.json["settings"]["discord_webhook_url"] == ""


@pytest.mark.parametrize("data", [
    {"discord_notifications_enabled": True},
    {"discord_notifications_enabled": None},
    {"discord_webhook_url": None},
    {"discord_webhook_url": "http://discord.com/api/webhooks/123/token"},
    {"discord_webhook_url": "https://evil.example/api/webhooks/123/token"},
    {"discord_webhook_url": "https://discord.com.evil.example/api/webhooks/123/token"},
    {"discord_webhook_url": "https://discord.com@127.0.0.1/api/webhooks/123/token"},
    {"discord_webhook_url": "https://discord.com:443/api/webhooks/123/token"},
    {"discord_webhook_url": f"{WEBHOOK}?redirect=1"},
    {"discord_webhook_url": "https://discord.com/channels/123"},
    {"unknown_notification_setting": True},
])
def test_settings_api_rejects_invalid(
    temp_dbs: None, client: TestApp, data: dict[str, object],
) -> None:
    response = client.put_json("/settings", data, status=422)
    assert response.json["code"] == "validation_error"
    assert settings.load_runtime_settings().discord_notifications_enabled is False
    assert settings.load_runtime_settings().discord_webhook_url == ""


def test_query_type_flags_partial_update_and_persistence(
    temp_dbs: None, client: TestApp,
) -> None:
    response = client.put_json("/settings", {"discord_query_types": {"A": False, " ptr ": False}})
    flags = response.json["settings"]["discord_query_types"]
    assert flags["A"] is False
    assert flags["PTR"] is False
    assert flags["AAAA"] is True
    assert flags["MX"] is True
    response = client.put_json("/settings", {"discord_query_types": {"AAAA": False, "MX": False}})
    flags = response.json["settings"]["discord_query_types"]
    assert all(flags[name] is False for name in ("A", "PTR", "AAAA", "MX"))
    client.put_json("/settings", {"discord_query_types": {}})
    db.reset_engines()
    flags = client.get("/settings").json["settings"]["discord_query_types"]
    assert all(flags[name] is False for name in ("A", "PTR", "AAAA", "MX"))
    response = client.put_json("/settings", {"discord_query_types": {"A": True}})
    assert response.json["settings"]["discord_query_types"]["A"] is True
    assert response.json["settings"]["discord_query_types"]["PTR"] is False


@pytest.mark.parametrize("flags", [
    None, [], "A", {"AAA": False}, {"NOTATYPE": False}, {"A": None},
    {"A": "false"}, {"A": 0}, {"A": False, "a": True},
    {"TYPE65536": False}, {"TYPE-1": False}, {"TYPE": False},
    {"A": False, "TYPE1": True},
])
def test_query_type_flags_reject_invalid_atomically(
    temp_dbs: None, client: TestApp, flags: object,
) -> None:
    response = client.put_json("/settings", {
        "discord_query_types": flags, "cache_enabled": False,
    }, status=422)
    assert response.json["code"] == "validation_error"
    values = client.get("/settings").json["settings"]
    assert values["cache_enabled"] is True
    assert all(values["discord_query_types"].values())


def test_numeric_query_type_flags_are_canonicalized(temp_dbs: None, client: TestApp) -> None:
    response = client.put_json("/settings", {
        "discord_query_types": {"TYPE1": False, "type65400": False},
    })
    flags = response.json["settings"]["discord_query_types"]
    assert flags["A"] is False
    assert flags["TYPE65400"] is False
    assert "TYPE1" not in flags


@pytest.mark.parametrize("raw", ['{"A": "false"}', "[]", "not-json", '{"AAA": false}'])
def test_corrupt_stored_query_flags_surface_error(
    temp_dbs: None, caplog: pytest.LogCaptureFixture, raw: str,
) -> None:
    with session_scope() as session:
        session.add(AppSetting(key="discord_query_types", value=raw))
    with pytest.raises(ValueError):
        settings.load_runtime_settings()
    assert "invalid stored Discord query type flags" in caplog.text


def test_runtime_query_flags_are_not_shared() -> None:
    first = RuntimeSettings()
    second = RuntimeSettings()
    first.discord_query_types["A"] = False
    assert second.discord_query_types["A"] is True


@pytest.mark.parametrize("same_batch", [False, True])
def test_filtered_pair_alerts_on_first_later_enabled_query(
    temp_dbs: None, monkeypatch: pytest.MonkeyPatch, same_batch: bool,
) -> None:
    _enable()
    settings.update_settings({"discord_query_types": {"A": False}})
    first = _event(action="deny", rcode="NXDOMAIN")
    second = _event(qtype="AAAA", action="override")
    sender = Mock()
    monkeypatch.setattr(notifications, "send_notification", sender)
    buffer = NotificationBuffer(notifications.record_observations)
    buffer.add(
        first.client, first.domain, first.qtype, first.action, first.rcode, True, False
    )
    if not same_batch:
        buffer.flush()
        notifications.deliver_pending()
        sender.assert_not_called()
        assert _rows()[0].status == "filtered"
        db.reset_engines()
        notifications.seed_seen_domains()
    buffer.add(
        second.client, second.domain, second.qtype, second.action, second.rcode, True, True
    )
    buffer.flush()
    notifications.deliver_pending()
    assert sender.call_count == 1
    sent = sender.call_args.args[1]
    assert sent.qtype == "AAAA"
    assert sent.action == "override"
    notifications.record_observations([_event(qtype="MX")])
    notifications.deliver_pending()
    assert sender.call_count == 1


@pytest.mark.parametrize("qtype", ["A", "AAAA", "PTR", "MX", "TXT", "HTTPS", "TYPE65400"])
def test_each_query_type_can_be_filtered(
    temp_dbs: None, monkeypatch: pytest.MonkeyPatch, qtype: str,
) -> None:
    _enable()
    settings.update_settings({"discord_query_types": {qtype: False}})
    notifications.record_observations([_event(qtype=qtype)])
    sender = Mock()
    monkeypatch.setattr(notifications, "send_notification", sender)
    notifications.deliver_pending()
    sender.assert_not_called()
    assert _rows()[0].status == "filtered"


def test_enabling_query_type_requires_a_new_observation(
    temp_dbs: None, monkeypatch: pytest.MonkeyPatch,
) -> None:
    _enable()
    settings.update_settings({"discord_query_types": {"A": False}})
    notifications.record_observations([_event()])
    settings.update_settings({"discord_query_types": {"A": True}})
    sender = Mock()
    monkeypatch.setattr(notifications, "send_notification", sender)
    notifications.deliver_pending()
    sender.assert_not_called()
    later = _event(action="deny", rcode="NXDOMAIN")
    notifications.record_observations([later])
    notifications.deliver_pending()
    assert sender.call_count == 1
    assert sender.call_args.args[1].first_seen == later.first_seen
    assert sender.call_args.args[1].action == "deny"


def test_disabling_type_filters_pending_until_later_eligible_query(
    temp_dbs: None, monkeypatch: pytest.MonkeyPatch,
) -> None:
    _enable()
    notifications.record_observations([_event()])
    settings.update_settings({"discord_query_types": {"A": False}})
    sender = Mock()
    monkeypatch.setattr(notifications, "send_notification", sender)
    notifications.deliver_pending()
    sender.assert_not_called()
    assert _rows()[0].status == "filtered"
    notifications.record_observations([_event(qtype="MX")])
    notifications.deliver_pending()
    assert sender.call_count == 1
    assert sender.call_args.args[1].qtype == "MX"


@pytest.mark.parametrize("disable_client", [False, True])
def test_disabling_monitoring_or_global_alerts_closes_filtered_pairs(
    temp_dbs: None, monkeypatch: pytest.MonkeyPatch, disable_client: bool,
) -> None:
    _enable()
    settings.update_settings({"discord_query_types": {"A": False}})
    hosts.record_hosts({"10.0.0.1": 1})
    notifications.record_observations([_event()])
    host_id = hosts.list_hosts()[0]["id"]
    if disable_client:
        hosts.update_host(host_id, {"new_domain_monitoring": False})
    else:
        settings.update_settings({"discord_notifications_enabled": False})
    notifications.deliver_pending()
    hosts.update_host(host_id, {"new_domain_monitoring": True})
    settings.update_settings({"discord_notifications_enabled": True})
    notifications.record_observations([_event(qtype="AAAA")])
    sender = Mock()
    monkeypatch.setattr(notifications, "send_notification", sender)
    notifications.deliver_pending()
    sender.assert_not_called()
    assert _rows()[0].status == "skipped"


def test_query_type_snapshot_prevents_delayed_alert_on_enable(
    temp_dbs: None, monkeypatch: pytest.MonkeyPatch,
) -> None:
    _enable()
    notifications.record_observations([_event(query_type_enabled=False)])
    sender = Mock()
    monkeypatch.setattr(notifications, "send_notification", sender)
    notifications.deliver_pending()
    sender.assert_not_called()
    notifications.record_observations([_event(query_type_enabled=True)])
    notifications.deliver_pending()
    assert sender.call_count == 1


def test_query_type_enable_between_queries_in_same_batch_is_not_lost(
    temp_dbs: None, monkeypatch: pytest.MonkeyPatch,
) -> None:
    _enable()
    buffer = NotificationBuffer(notifications.record_observations)
    buffer.add("10.0.0.1", "example.com", "A", "deny", "NXDOMAIN", True, False)
    buffer.add("10.0.0.1", "example.com", "A", "forward", "NOERROR", True, True)
    buffer.flush()
    sender = Mock()
    monkeypatch.setattr(notifications, "send_notification", sender)
    notifications.deliver_pending()
    assert sender.call_count == 1
    assert sender.call_args.args[1].action == "forward"
    assert sender.call_args.args[1].rcode == "NOERROR"


def test_monitoring_alias_controls_feed_and_notifications(
    temp_dbs: None, client: TestApp, monkeypatch: pytest.MonkeyPatch,
) -> None:
    _enable()
    hosts.record_hosts({"10.0.0.1": 1})
    host_id = hosts.list_hosts()[0]["id"]
    response = client.put_json(f"/hosts/{host_id}", {"new_domain_monitoring": False})
    assert response.json["host"]["flag_new_domains"] is False
    assert response.json["host"]["new_domain_monitoring"] is False
    assert hosts.load_new_domain_unmonitored() == frozenset({"10.0.0.1"})
    notifications.record_observations([_event()])
    sender = Mock()
    monkeypatch.setattr(notifications, "send_notification", sender)
    notifications.deliver_pending()
    sender.assert_not_called()
    assert _rows()[0].status == "skipped"
    response = client.put_json(f"/hosts/{host_id}", {"flag_new_domains": True})
    assert response.json["host"]["new_domain_monitoring"] is True
    client.put_json(f"/hosts/{host_id}", {
        "flag_new_domains": True, "new_domain_monitoring": False,
    }, status=422)
    notifications.record_observations([_event()])
    notifications.deliver_pending()
    sender.assert_not_called()


def test_first_pair_once_across_qtypes_batches_and_restart(
    temp_dbs: None, monkeypatch: pytest.MonkeyPatch,
) -> None:
    _enable()
    notifications.record_observations([
        _event(action="deny", rcode="NXDOMAIN"), _event(qtype="AAAA"),
        _event(client="10.0.0.2"),
    ])
    sender = Mock()
    monkeypatch.setattr(notifications, "send_notification", sender)
    notifications.deliver_pending()
    notifications.deliver_pending()
    assert sender.call_count == 2
    first = sender.call_args_list[0].args[1]
    assert first.action == "deny"
    assert first.rcode == "NXDOMAIN"
    db.reset_engines()
    notifications.seed_seen_domains()
    notifications.record_observations([_event()])
    notifications.deliver_pending()
    assert sender.call_count == 2
    assert [row.status for row in _rows()] == ["sent", "sent"]


def test_existing_history_is_baselined_without_alerts(
    temp_dbs: None, monkeypatch: pytest.MonkeyPatch,
) -> None:
    _enable()
    old = datetime.now() - timedelta(days=1)
    with session_scope() as session:
        session.add_all([
            ClientRequest(client="10.0.0.1", domain="example.com", qtype=qtype,
                          hits=1, first_seen=old, last_seen=old)
            for qtype in ("A", "AAAA")
        ])
    notifications.seed_seen_domains()
    notifications.seed_seen_domains()
    notifications.record_observations([_event()])
    sender = Mock()
    monkeypatch.setattr(notifications, "send_notification", sender)
    notifications.deliver_pending()
    sender.assert_not_called()
    assert len(_rows()) == 1
    assert _rows()[0].first_seen == old


def test_disabled_observations_do_not_replay_on_enable(
    temp_dbs: None, monkeypatch: pytest.MonkeyPatch,
) -> None:
    notifications.record_observations([_event()])
    _enable()
    notifications.record_observations([_event(), _event(domain="disabled.example", enabled=False)])
    sender = Mock()
    monkeypatch.setattr(notifications, "send_notification", sender)
    notifications.deliver_pending()
    sender.assert_not_called()
    assert all(row.status == "skipped" for row in _rows())


@pytest.mark.parametrize("disable_client", [False, True])
def test_disable_cancels_queued_messages(
    temp_dbs: None, monkeypatch: pytest.MonkeyPatch, disable_client: bool,
) -> None:
    _enable()
    hosts.record_hosts({"10.0.0.1": 1})
    notifications.record_observations([_event()])
    if disable_client:
        hosts.update_host(hosts.list_hosts()[0]["id"], {"flag_new_domains": False})
    else:
        settings.update_settings({"discord_notifications_enabled": False})
    sender = Mock()
    monkeypatch.setattr(notifications, "send_notification", sender)
    notifications.deliver_pending()
    sender.assert_not_called()
    assert _rows()[0].status == "skipped"


def test_retry_is_persistent_and_bounded(temp_dbs: None, monkeypatch: pytest.MonkeyPatch) -> None:
    _enable()
    notifications.record_observations([_event()])
    sender = Mock(side_effect=discord.DiscordDeliveryError("HTTP 429", 12))
    monkeypatch.setattr(notifications, "send_notification", sender)
    assert notifications.deliver_pending() == 12
    assert _rows()[0].status == "pending"
    assert _rows()[0].attempts == 1
    notifications.deliver_pending()
    assert sender.call_count == 1
    db.reset_engines()
    for _ in range(4):
        with session_scope() as session:
            row = session.scalars(select(DomainNotification)).one()
            row.next_attempt = datetime.now() - timedelta(seconds=1)
        notifications.deliver_pending()
    assert sender.call_count == 5
    assert _rows()[0].status == "failed"
    notifications.deliver_pending()
    assert sender.call_count == 5


def test_pending_delivery_succeeds_after_restart(
    temp_dbs: None, monkeypatch: pytest.MonkeyPatch,
) -> None:
    _enable()
    notifications.record_observations([_event()])
    db.reset_engines()
    notifications.seed_seen_domains()
    sender = Mock()
    monkeypatch.setattr(notifications, "send_notification", sender)
    notifications.deliver_pending()
    assert sender.call_count == 1
    assert _rows()[0].status == "sent"


def test_background_worker_persists_delivers_and_flushes_on_stop(
    temp_dbs: None, monkeypatch: pytest.MonkeyPatch,
) -> None:
    _enable()
    delivered = threading.Event()
    sender = Mock(side_effect=lambda *args, **kwargs: delivered.set())
    monkeypatch.setattr(notifications, "send_notification", sender)
    buffer = NotificationBuffer(notifications.record_observations)
    worker = NotificationWorker(buffer)
    buffer.add("10.0.0.1", "first.example", "A", "forward", "NOERROR", True)
    worker.start()
    try:
        assert delivered.wait(10), "notification worker did not deliver"
        buffer.add("10.0.0.1", "second.example", "A", "deny", "NXDOMAIN", True)
    finally:
        worker.stop()
    assert sender.call_count == 1
    assert [row.status for row in _rows()] == ["sent", "pending"]


def test_permanent_error_not_retried(temp_dbs: None, monkeypatch: pytest.MonkeyPatch) -> None:
    _enable()
    notifications.record_observations([_event()])
    sender = Mock(side_effect=discord.DiscordDeliveryError("HTTP 404"))
    monkeypatch.setattr(notifications, "send_notification", sender)
    notifications.deliver_pending()
    notifications.deliver_pending()
    assert sender.call_count == 1
    assert _rows()[0].status == "failed"


def test_host_delete_resets_ledger(temp_dbs: None) -> None:
    hosts.record_hosts({"10.0.0.1": 1})
    notifications.record_observations([_event()])
    assert hosts.delete_host(hosts.list_hosts()[0]["id"]) is True
    assert _rows() == []


@pytest.mark.parametrize("name", [None, "SANDO CONTAINER"])
def test_delivery_uses_current_client_name(
    temp_dbs: None, monkeypatch: pytest.MonkeyPatch, name: str | None,
) -> None:
    _enable()
    notifications.record_observations([_event(client="10.4.40.40")])
    if name is not None:
        hosts.record_hosts({"10.4.40.40": 1})
        hosts.update_host_by_ip("10.4.40.40", {"device_name": name})
    sender = Mock()
    monkeypatch.setattr(notifications, "send_notification", sender)
    notifications.deliver_pending()
    assert sender.call_args.kwargs == {"client_name": name}
    assert sender.call_args.args[1].client == "10.4.40.40"
    assert _rows()[0].status == "sent"


def test_buffer_keeps_first_observation_and_retries_failed_flush() -> None:
    captured: list[DomainObservation] = []
    attempts = 0

    def flush(rows: list[DomainObservation]) -> None:
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise RuntimeError("database unavailable")
        captured.extend(rows)

    buffer = NotificationBuffer(flush)
    buffer.add("10.0.0.1", "example.com", "A", "deny", "NXDOMAIN", True)
    buffer.add("10.0.0.1", "example.com", "A", "forward", "NOERROR", True)
    buffer.flush()
    buffer.add("10.0.0.1", "example.com", "A", "forward", "NOERROR", True)
    buffer.flush()
    assert len(captured) == 1
    assert captured[0].action == "deny"


def test_buffer_overflow_is_logged(caplog: pytest.LogCaptureFixture) -> None:
    captured: list[DomainObservation] = []
    buffer = NotificationBuffer(captured.extend, max_events=1)
    buffer.add("10.0.0.1", "first.com", "A", "forward", "NOERROR", True)
    buffer.add("10.0.0.1", "second.com", "A", "forward", "NOERROR", True)
    buffer.flush()
    assert [event.domain for event in captured] == ["first.com"]
    assert "notification capture full" in caplog.text


@pytest.mark.parametrize("action,rcode,outcome", [
    ("forward", "NOERROR", "ALLOW"), ("forward-cache", "NOERROR", "ALLOW"),
    ("override", "NOERROR", "ALLOW"), ("local", "NOERROR", "ALLOW"),
    ("nodata", "NOERROR", "ALLOW"), ("forward", "NXDOMAIN", "ALLOW"),
    ("deny", "NXDOMAIN", "BLOCK"), ("blocklist", "NXDOMAIN", "BLOCK"),
    ("servfail", "SERVFAIL", "ERROR"), ("forward", "REFUSED", "ERROR"),
])
@pytest.mark.parametrize("name,label", [
    (None, "10.0.0.1"),
    ("", "10.0.0.1"),
    (" \n ", "10.0.0.1"),
    ("SANDO CONTAINER", "SANDO CONTAINER (10.0.0.1)"),
    (" SANDO\nCONTAINER ", "SANDO CONTAINER (10.0.0.1)"),
    ("**SANDO** `CONTAINER`", r"\*\*SANDO\*\* \`CONTAINER\` (10.0.0.1)"),
])
def test_discord_payload(
    monkeypatch: pytest.MonkeyPatch, action: str, rcode: str, outcome: str,
    name: str | None, label: str,
) -> None:
    import json

    response = Mock(status=204)
    response.__enter__ = Mock(return_value=response)
    response.__exit__ = Mock(return_value=False)
    opener = Mock()
    opener.open.return_value = response
    monkeypatch.setattr(discord, "build_opener", lambda *args: opener)
    event = _event(action=action, rcode=rcode)
    discord.send_notification(WEBHOOK, event, client_name=name)
    request = opener.open.call_args.args[0]
    payload = json.loads(request.data)
    icon = {"ALLOW": "\u2705", "BLOCK": "\U0001f6d1", "ERROR": "\u26a0\ufe0f"}[outcome]
    assert payload["content"] == (
        f"{icon} **{outcome}**: **{label}** queried **example.com** "
        f"for the first time (A; action: {action}; DNS: {rcode}; "
        f"first seen: {event.first_seen.isoformat()})."
    )
    assert "\n" not in payload["content"]
    assert len(payload["content"]) <= 2000
    assert payload["allowed_mentions"] == {"parse": []}
    assert opener.open.call_args.kwargs["timeout"] == 5
    assert "/u/homelabids" in request.get_header("User-agent")


@pytest.mark.parametrize("status,retry", [(429, 17.0), (500, 5.0), (404, None), (302, None)])
def test_http_errors_are_sanitized(
    monkeypatch: pytest.MonkeyPatch, status: int, retry: float | None,
) -> None:
    opener = Mock()
    opener.open.side_effect = HTTPError(WEBHOOK, status, "secret_token", {"Retry-After": "17"}, None)
    monkeypatch.setattr(discord, "build_opener", lambda *args: opener)
    with pytest.raises(discord.DiscordDeliveryError) as raised:
        discord.send_notification(WEBHOOK, _event())
    assert str(raised.value) == f"HTTP {status}"
    assert raised.value.retry_seconds == retry
    assert raised.value.__suppress_context__ is True


def test_network_error_is_sanitized(monkeypatch: pytest.MonkeyPatch) -> None:
    opener = Mock()
    opener.open.side_effect = URLError(WEBHOOK)
    monkeypatch.setattr(discord, "build_opener", lambda *args: opener)
    with pytest.raises(discord.DiscordDeliveryError, match="network error"):
        discord.send_notification(WEBHOOK, _event())


def test_redirect_is_not_followed() -> None:
    assert discord._NoRedirect().redirect_request(
        Mock(), Mock(), 302, "redirect", {}, "https://evil.example",
    ) is None


@pytest.mark.parametrize("action", ["deny", "override"])
def test_resolver_captures_final_outcome_without_http(action: str) -> None:
    captured: list[DomainObservation] = []
    buffer = NotificationBuffer(captured.extend)
    resolver = _make_resolver(
        [PolicyRule("*", "*", action, override_response="192.0.2.1")],
        RuntimeSettings(discord_notifications_enabled=True), notifications=buffer,
    )
    resolver.handle(DNSRecord.question("EXAMPLE.COM.", "A"), "10.0.0.1")
    assert captured == []
    buffer.flush()
    assert captured[0].domain == "example.com"
    assert captured[0].action == action
    assert captured[0].rcode == ("NXDOMAIN" if action == "deny" else "NOERROR")
    assert captured[0].notifications_enabled is True


def test_skipped_private_ptr_and_foreign_client_not_notified() -> None:
    captured: list[DomainObservation] = []
    buffer = NotificationBuffer(captured.extend)
    resolver = _make_resolver([], notifications=buffer)
    resolver.handle(DNSRecord.question("1.0.0.10.in-addr.arpa", "PTR"), "10.0.0.1")
    resolver.record_foreign("203.0.113.1")
    buffer.flush()
    assert captured == []


@pytest.mark.parametrize("qtype", ["A", "AAAA", "PTR", "MX", "TYPE65400"])
def test_resolver_captures_query_type_flag(qtype: str) -> None:
    captured: list[DomainObservation] = []
    buffer = NotificationBuffer(captured.extend)
    resolver = _make_resolver(
        [PolicyRule("*", "*", "deny")],
        RuntimeSettings(
            discord_notifications_enabled=True, discord_query_types={qtype: False},
        ),
        notifications=buffer,
    )
    question = DNSRecord.question("example.com", "A")
    question.q.qtype = 65400 if qtype == "TYPE65400" else QTYPE.reverse[qtype]
    resolver.handle(question, "10.0.0.1")
    buffer.flush()
    assert captured[0].qtype == qtype
    assert captured[0].query_type_enabled is False
    assert captured[0].notifications_enabled is True
