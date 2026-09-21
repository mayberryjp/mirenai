import os

os.environ.setdefault("DATABASE_URL", "sqlite://")

import pytest
from webtest import TestApp

from mirenai.api.app import create_app


@pytest.fixture()
def client() -> TestApp:
    return TestApp(create_app())
