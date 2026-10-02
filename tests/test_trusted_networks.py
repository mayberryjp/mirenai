from collections.abc import Iterator

import pytest
from sqlalchemy.exc import IntegrityError
from webtest import TestApp

from mirenai import config, db
from mirenai.api.app import create_app
from mirenai.db import Base
from mirenai.repository import trusted_networks as repo


@pytest.fixture()
def temp_config_db(tmp_path, monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    db_path = tmp_path / "mirenai.db"
    monkeypatch.setattr(config.settings, "database_url", f"sqlite:///{db_path}")
    # Force the cached engine/session factory to rebuild against the temp DB.
    monkeypatch.setattr(db, "_engine", None)
    monkeypatch.setattr(db, "_session_factory", None)
    Base.metadata.create_all(db.get_engine())
    yield


def test_create_and_list(temp_config_db: None) -> None:
    repo.create_trusted_network({"cidr": "10.2.10.0/24", "description": "lan"})
    rows = repo.list_trusted_networks()
    assert len(rows) == 1
    assert rows[0]["cidr"] == "10.2.10.0/24"
    assert rows[0]["description"] == "lan"


def test_load_trusted_networks_returns_cidrs(temp_config_db: None) -> None:
    repo.create_trusted_network({"cidr": "10.2.10.0/24"})
    repo.create_trusted_network({"cidr": "192.168.0.0/16"})
    assert set(repo.load_trusted_networks()) == {"10.2.10.0/24", "192.168.0.0/16"}


def test_duplicate_cidr_raises(temp_config_db: None) -> None:
    repo.create_trusted_network({"cidr": "10.2.10.0/24"})
    with pytest.raises(IntegrityError):
        repo.create_trusted_network({"cidr": "10.2.10.0/24"})


def test_delete_trusted_network(temp_config_db: None) -> None:
    row = repo.create_trusted_network({"cidr": "10.2.10.0/24"})
    assert repo.delete_trusted_network(row["id"]) is True
    assert repo.delete_trusted_network(row["id"]) is False


def test_api_list_empty(temp_config_db: None) -> None:
    app = TestApp(create_app())
    resp = app.get("/trusted-networks")
    assert resp.status_code == 200
    assert resp.json == {"status": "ok", "trusted_networks": [], "total": 0}


def test_api_create_valid(temp_config_db: None) -> None:
    app = TestApp(create_app())
    resp = app.post_json("/trusted-networks", {"cidr": "10.2.10.0/24", "description": "lan"})
    assert resp.status_code == 201
    assert resp.json["trusted_network"]["cidr"] == "10.2.10.0/24"
    assert resp.json["trusted_network"]["description"] == "lan"


def test_api_create_normalizes_host_bits(temp_config_db: None) -> None:
    app = TestApp(create_app())
    resp = app.post_json("/trusted-networks", {"cidr": "10.2.10.5/24"})
    assert resp.status_code == 201
    assert resp.json["trusted_network"]["cidr"] == "10.2.10.0/24"


def test_api_create_invalid_cidr(temp_config_db: None) -> None:
    app = TestApp(create_app())
    resp = app.post_json("/trusted-networks", {"cidr": "not-a-subnet"}, status=422)
    assert resp.json["code"] == "validation_error"


def test_api_create_duplicate_conflict(temp_config_db: None) -> None:
    app = TestApp(create_app())
    app.post_json("/trusted-networks", {"cidr": "10.2.10.0/24"})
    resp = app.post_json("/trusted-networks", {"cidr": "10.2.10.0/24"}, status=409)
    assert resp.json["code"] == "conflict"


def test_api_delete(temp_config_db: None) -> None:
    app = TestApp(create_app())
    created = app.post_json("/trusted-networks", {"cidr": "10.2.10.0/24"})
    network_id = created.json["trusted_network"]["id"]
    resp = app.delete(f"/trusted-networks/{network_id}")
    assert resp.status_code == 200
    assert resp.json == {"status": "ok", "deleted": network_id}


def test_api_delete_missing(temp_config_db: None) -> None:
    app = TestApp(create_app())
    resp = app.delete("/trusted-networks/9999", status=404)
    assert resp.json["code"] == "not_found"
