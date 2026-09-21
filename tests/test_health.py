from webtest import TestApp


def test_health_ok(client: TestApp) -> None:
    resp = client.get("/health")
    assert resp.status_code == 200
    assert resp.json["status"] == "ok"
    assert resp.json["service"] == "mirenai-api"
