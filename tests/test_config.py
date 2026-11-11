import pytest

from app import __main__ as entrypoint
from app.config import DEFAULT_REDIRECT_URIS, ConfigError, Settings
from tests.conftest import FAKE_ENV


def test_minimal_env_uses_defaults():
    s = Settings.from_env(FAKE_ENV)
    assert s.base_url == "https://bridge.test"
    assert s.bot_token == "fake-bot-token"
    assert s.admin_tg_id is None
    assert s.daily_message_limit == 200
    assert s.allowed_redirect_uris == DEFAULT_REDIRECT_URIS


@pytest.mark.parametrize("key", ["BASE_URL", "BOT_TOKEN"])
def test_missing_required_key_fails(key):
    env = {k: v for k, v in FAKE_ENV.items() if k != key}
    with pytest.raises(ConfigError, match=key):
        Settings.from_env(env)


def test_blank_required_key_fails():
    with pytest.raises(ConfigError, match="BOT_TOKEN"):
        Settings.from_env({**FAKE_ENV, "BOT_TOKEN": "   "})


def test_base_url_trailing_slash_stripped():
    s = Settings.from_env({**FAKE_ENV, "BASE_URL": "https://bridge.test/"})
    assert s.base_url == "https://bridge.test"


def test_base_url_requires_scheme():
    with pytest.raises(ConfigError, match="BASE_URL"):
        Settings.from_env({**FAKE_ENV, "BASE_URL": "bridge.test"})


def test_invalid_integer_fails():
    with pytest.raises(ConfigError, match="ACCESS_TOKEN_TTL"):
        Settings.from_env({**FAKE_ENV, "ACCESS_TOKEN_TTL": "soon"})


def test_optional_values_parsed():
    s = Settings.from_env(
        {
            **FAKE_ENV,
            "ADMIN_TG_ID": "42",
            "BOT_USERNAME": "@SomeBot",
            "ALLOWED_REDIRECT_URIS": "https://a.test/cb, https://b.test/cb",
            "LOG_LEVEL": "debug",
        }
    )
    assert s.admin_tg_id == 42
    assert s.bot_username == "SomeBot"
    assert s.allowed_redirect_uris == ("https://a.test/cb", "https://b.test/cb")
    assert s.log_level == "DEBUG"


def test_entrypoint_exits_with_clear_error(monkeypatch, capsys):
    monkeypatch.delenv("BOT_TOKEN", raising=False)
    monkeypatch.setenv("BASE_URL", "https://bridge.test")
    with pytest.raises(SystemExit) as exc:
        entrypoint.main()
    assert exc.value.code == 1
    assert "missing required variables: BOT_TOKEN" in capsys.readouterr().err
