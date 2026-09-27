import json
from urllib.error import HTTPError, URLError

import pytest

from mirenai.integrations import sando


class _FakeResponse:
    def __init__(self, payload: bytes) -> None:
        self._payload = payload

    def __enter__(self) -> "_FakeResponse":
        return self

    def __exit__(self, *exc: object) -> None:
        return None

    def read(self) -> bytes:
        return self._payload


def _configure(monkeypatch: pytest.MonkeyPatch, url: str = "http://sando.test") -> None:
    monkeypatch.setattr(sando.settings, "sando_api_url", url)


def test_fetch_device_parses_name_and_icon(monkeypatch: pytest.MonkeyPatch) -> None:
    _configure(monkeypatch)
    payload = json.dumps({"local_description": "Living Room TV", "icon": "television_icon"}).encode()
    monkeypatch.setattr(sando, "urlopen", lambda *a, **k: _FakeResponse(payload))
    device = sando.fetch_device("10.0.0.5")
    assert device is not None
    assert device.device_name == "Living Room TV"
    assert device.icon == "television_icon"


def test_fetch_device_blank_fields_become_none(monkeypatch: pytest.MonkeyPatch) -> None:
    _configure(monkeypatch)
    payload = json.dumps({"local_description": "  ", "icon": ""}).encode()
    monkeypatch.setattr(sando, "urlopen", lambda *a, **k: _FakeResponse(payload))
    assert sando.fetch_device("10.0.0.5") == sando.SandoDevice(device_name=None, icon=None)


def test_fetch_device_unknown_host_returns_none(monkeypatch: pytest.MonkeyPatch) -> None:
    _configure(monkeypatch)

    def _raise(*a: object, **k: object) -> None:
        raise HTTPError("http://sando.test", 404, "Not Found", {}, None)  # type: ignore[arg-type]

    monkeypatch.setattr(sando, "urlopen", _raise)
    assert sando.fetch_device("10.0.0.5") is None


def test_fetch_device_error_payload_returns_none(monkeypatch: pytest.MonkeyPatch) -> None:
    _configure(monkeypatch)
    payload = json.dumps({"error": "No local host found"}).encode()
    monkeypatch.setattr(sando, "urlopen", lambda *a, **k: _FakeResponse(payload))
    assert sando.fetch_device("10.0.0.5") is None


def test_fetch_device_transport_error_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    _configure(monkeypatch)

    def _raise(*a: object, **k: object) -> None:
        raise URLError("connection refused")

    monkeypatch.setattr(sando, "urlopen", _raise)
    with pytest.raises(sando.SandoError):
        sando.fetch_device("10.0.0.5")


def test_fetch_device_server_error_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    _configure(monkeypatch)

    def _raise(*a: object, **k: object) -> None:
        raise HTTPError("http://sando.test", 500, "Server Error", {}, None)  # type: ignore[arg-type]

    monkeypatch.setattr(sando, "urlopen", _raise)
    with pytest.raises(sando.SandoError):
        sando.fetch_device("10.0.0.5")


def test_sync_host_not_configured_is_noop(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(sando.settings, "sando_api_url", "")
    calls: list[tuple[str, dict[str, object]]] = []
    monkeypatch.setattr(sando, "update_host_by_ip", lambda ip, data: calls.append((ip, data)))
    assert sando.sync_host_from_sando("10.0.0.5") is None
    assert calls == []


def test_sync_host_applies_fields(monkeypatch: pytest.MonkeyPatch) -> None:
    _configure(monkeypatch)
    monkeypatch.setattr(
        sando, "fetch_device", lambda ip: sando.SandoDevice(device_name="TV", icon="tv_icon")
    )
    captured: dict[str, object] = {}

    def _update(ip: str, data: dict[str, object]) -> dict[str, object]:
        captured["ip"] = ip
        captured["data"] = data
        return {"id": 1, "ip": ip, **data}

    monkeypatch.setattr(sando, "update_host_by_ip", _update)
    result = sando.sync_host_from_sando("10.0.0.5")
    assert captured["ip"] == "10.0.0.5"
    assert captured["data"] == {"device_name": "TV", "icon": "tv_icon"}
    assert result is not None
    assert result["device_name"] == "TV"


def test_sync_host_found_but_empty_still_returns_host(monkeypatch: pytest.MonkeyPatch) -> None:
    _configure(monkeypatch)
    monkeypatch.setattr(
        sando, "fetch_device", lambda ip: sando.SandoDevice(device_name=None, icon=None)
    )
    captured: dict[str, object] = {}

    def _update(ip: str, data: dict[str, object]) -> dict[str, object]:
        captured["ip"] = ip
        captured["data"] = data
        return {"id": 1, "ip": ip, "device_name": None, "icon": None}

    monkeypatch.setattr(sando, "update_host_by_ip", _update)
    result = sando.sync_host_from_sando("10.0.0.5")
    # Sando HAS the host (just no name/icon) -> not a "missing" host; nothing to apply.
    assert captured["data"] == {}
    assert result is not None
    assert result["ip"] == "10.0.0.5"


def test_sync_host_unknown_device_returns_none(monkeypatch: pytest.MonkeyPatch) -> None:
    _configure(monkeypatch)
    monkeypatch.setattr(sando, "fetch_device", lambda ip: None)
    calls: list[tuple[str, dict[str, object]]] = []
    monkeypatch.setattr(sando, "update_host_by_ip", lambda ip, data: calls.append((ip, data)))
    assert sando.sync_host_from_sando("10.0.0.5") is None
    assert calls == []
