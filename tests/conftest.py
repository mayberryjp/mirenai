import os

os.environ.setdefault("CONFIG_DATABASE_URL", "sqlite://")
os.environ.setdefault("STATS_DATABASE_URL", "sqlite://")
os.environ.setdefault("CACHE_DATABASE_URL", "sqlite://")
os.environ.setdefault("QUERYLOG_DATABASE_URL", "sqlite://")
os.environ.setdefault("BLOCKLIST_DATABASE_URL", "sqlite://")
os.environ.setdefault("LOCALHOSTS_DATABASE_URL", "sqlite://")
os.environ.setdefault("DATABASE_URL", "sqlite://")

from collections.abc import Iterator

import pytest
from webtest import TestApp

from mirenai import db
from mirenai.api.app import create_app


@pytest.fixture(autouse=True)
def _reset_db_engines() -> Iterator[None]:
    """Give every test fresh engines so a temp-DB fixture never reuses a stale file."""
    db.reset_engines()
    yield
    db.reset_engines()


@pytest.fixture()
def client() -> TestApp:
    return TestApp(create_app())
