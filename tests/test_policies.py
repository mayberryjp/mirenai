from pathlib import Path

import pytest
from webtest import TestApp

from mirenai import db


@pytest.fixture()
def policy_client(
    client: TestApp, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> TestApp:
    monkeypatch.setenv("CONFIG_DATABASE_URL", f"sqlite:///{tmp_path / 'config.db'}")
    db.create_all_schemas()
    return client


def test_filter_overrides_and_paginate(policy_client: TestApp) -> None:
    for domain, action in [("one.test", "override"), ("two.test", "deny"), ("three.test", "override")]:
        policy_client.post_json("/policies", {
            "client": "*", "domain": domain, "action": action,
            "override_response": "10.0.0.53" if action == "override" else None,
        }, status=201)
    all_rows = policy_client.get("/policies").json
    assert all_rows["total"] == 3
    filtered = policy_client.get("/policies?action=override").json
    assert filtered["total"] == 2
    assert [row["domain"] for row in filtered["policies"]] == ["one.test", "three.test"]
    page = policy_client.get("/policies?action=override&limit=1&offset=1").json
    assert page["total"] == 2
    assert [row["domain"] for row in page["policies"]] == ["three.test"]
    assert policy_client.get("/policies?action=override&offset=1").json["policies"] == page["policies"]


def test_invalid_action_filter(policy_client: TestApp) -> None:
    result = policy_client.get("/policies?action=spoof", status=422).json
    assert result["code"] == "validation_error"


def test_override_lifecycle(policy_client: TestApp) -> None:
    created = policy_client.post_json("/policies", {
        "client": "*", "domain": "service.test", "action": "override",
        "override_response": "10.0.0.53, fd00::53",
    }, status=201).json["policy"]
    path = f"/policies/{created['id']}"
    assert created["override_ttl"] == 300
    assert created["enabled"] is True
    assert policy_client.get(path).json["policy"] == created
    updated = policy_client.put_json(path, {
        "client": "10.0.0.20", "domain": "*.internal.test",
        "override_response": "10.0.0.54", "override_ttl": 60,
        "description": "Internal service", "enabled": False,
    }).json["policy"]
    assert updated["domain"] == "*.internal.test"
    assert updated["override_response"] == "10.0.0.54"
    assert updated["override_ttl"] == 60
    assert updated["client"] == "10.0.0.20"
    assert updated["description"] == "Internal service"
    assert updated["enabled"] is False
    assert policy_client.get("/policies?action=override").json["policies"] == [updated]
    assert policy_client.put_json(path, {"enabled": True, "description": None}).json["policy"]["enabled"] is True
    assert policy_client.delete(path).json == {"status": "ok", "deleted": created["id"]}
    policy_client.get(path, status=404)
    policy_client.put_json(path, {"enabled": False}, status=404)
    policy_client.delete(path, status=404)


@pytest.mark.parametrize("changes", [
    {"override_response": None}, {"override_response": ""},
    {"override_response": "invalid"}, {"override_response": "10.0.0.53,"},
    {"override_ttl": -1}, {"override_ttl": 2147483648},
    {"override_ttl": None}, {"domain": " "}, {"domain": None},
    {"client": None}, {"action": None}, {"enabled": None},
])
def test_invalid_override_update_is_not_saved(policy_client: TestApp, changes: dict) -> None:
    original = policy_client.post_json("/policies", {
        "client": "*", "domain": "service.test", "action": "override",
        "override_response": "10.0.0.53",
    }, status=201).json["policy"]
    path = f"/policies/{original['id']}"
    assert policy_client.put_json(path, changes, status=422).json["code"] == "validation_error"
    assert policy_client.get(path).json["policy"] == original


@pytest.mark.parametrize("answer", [None, "", "not-an-ip", "10.0.0.53,", "10.0.0.53,invalid"])
def test_invalid_override_create(policy_client: TestApp, answer: str | None) -> None:
    policy_client.post_json("/policies", {
        "client": "*", "domain": "service.test", "action": "override",
        "override_response": answer,
    }, status=422)


def test_switch_action_and_conflicts(policy_client: TestApp) -> None:
    body = {"client": "*", "domain": "service.test", "action": "forward"}
    created = policy_client.post_json("/policies", body, status=201).json["policy"]
    path = f"/policies/{created['id']}"
    policy_client.post_json("/policies", body, status=409)
    policy_client.put_json(path, {"action": "override"}, status=422)
    policy_client.put_json(path, {"action": "override", "override_response": "::1", "override_ttl": 0})
    policy_client.post_json("/policies", {**body, "domain": "other.test"}, status=201)
    policy_client.put_json(path, {"domain": "other.test"}, status=409)
    policy_client.put_json(path, {"action": "forward", "override_response": None})
    assert policy_client.get("/policies?action=override").json["total"] == 0