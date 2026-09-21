import pytest
from webtest import TestApp

from mirenai.api.routes import health


def test_ready_ok(client: TestApp, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(health, "check_database", lambda: (True, "ok"))
    resp = client.get("/ready")
    assert resp.status_code == 200
    assert resp.json["status"] == "ok"


def test_ready_db_down(client: TestApp, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(health, "check_database", lambda: (False, "database check failed: OperationalError"))
    resp = client.get("/ready", expect_errors=True)
    assert resp.status_code == 503
    assert resp.json["code"] == "not_ready"
