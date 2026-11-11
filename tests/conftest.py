import pytest
from starlette.testclient import TestClient

from app.config import Settings
from app.db import Database, sqlite_url
from app.main import create_app

FAKE_ENV = {
    "BASE_URL": "https://bridge.test",
    "BOT_TOKEN": "fake-bot-token",
}


@pytest.fixture
def settings(tmp_path) -> Settings:
    return Settings.from_env({**FAKE_ENV, "DB_PATH": str(tmp_path / "db.sqlite")})


@pytest.fixture
def client(settings: Settings) -> TestClient:
    with TestClient(create_app(settings)) as c:
        yield c


@pytest.fixture
def db(tmp_path) -> Database:
    database = Database(sqlite_url(str(tmp_path / "test.sqlite")))
    database.init_schema()
    yield database
    database.close()
