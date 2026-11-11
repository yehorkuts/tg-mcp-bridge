import pytest
from starlette.testclient import TestClient

from app.config import Settings
from app.main import create_app

FAKE_ENV = {
    "BASE_URL": "https://bridge.test",
    "BOT_TOKEN": "fake-bot-token",
}


@pytest.fixture
def settings() -> Settings:
    return Settings.from_env(FAKE_ENV)


@pytest.fixture
def client(settings: Settings) -> TestClient:
    with TestClient(create_app(settings)) as c:
        yield c
