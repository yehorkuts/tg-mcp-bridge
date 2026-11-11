"""Application settings loaded from environment variables."""

import os
from collections.abc import Mapping
from dataclasses import dataclass

DEFAULT_REDIRECT_URIS = (
    "https://claude.ai/api/mcp/auth_callback",
    "https://claude.com/api/mcp/auth_callback",
    "http://localhost:6274/oauth/callback",
)

REQUIRED_KEYS = ("BASE_URL", "BOT_TOKEN")


class ConfigError(Exception):
    """Raised when the environment does not describe a valid configuration."""


@dataclass(frozen=True)
class Settings:
    base_url: str
    bot_token: str
    domain: str = ""
    bot_username: str = ""
    tg_webhook_secret: str = ""
    admin_tg_id: int | None = None
    daily_message_limit: int = 200
    daily_poll_limit: int = 100
    digest_hour: int = 9
    tz: str = "Europe/Warsaw"
    db_path: str = "/data/db.sqlite"
    allowed_redirect_uris: tuple[str, ...] = DEFAULT_REDIRECT_URIS
    access_token_ttl: int = 3600
    refresh_token_ttl: int = 2592000
    log_level: str = "INFO"

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None) -> "Settings":
        """Build settings from a mapping (defaults to os.environ).

        Raises ConfigError listing every problem found, so a bad deployment
        fails at startup with one clear message.
        """
        env = os.environ if env is None else env
        errors: list[str] = []

        def get(key: str) -> str:
            return env.get(key, "").strip()

        missing = [key for key in REQUIRED_KEYS if not get(key)]
        if missing:
            errors.append(f"missing required variables: {', '.join(missing)}")

        def get_int(key: str, default: int | None) -> int | None:
            raw = get(key)
            if not raw:
                return default
            try:
                return int(raw)
            except ValueError:
                errors.append(f"{key} must be an integer, got {raw!r}")
                return default

        base_url = get("BASE_URL").rstrip("/")
        if base_url and not base_url.startswith(("http://", "https://")):
            errors.append("BASE_URL must start with http:// or https://")

        redirect_raw = get("ALLOWED_REDIRECT_URIS")
        redirect_uris = (
            tuple(u.strip() for u in redirect_raw.split(",") if u.strip())
            if redirect_raw
            else DEFAULT_REDIRECT_URIS
        )

        settings = cls(
            base_url=base_url,
            bot_token=get("BOT_TOKEN"),
            domain=get("DOMAIN"),
            bot_username=get("BOT_USERNAME").lstrip("@"),
            tg_webhook_secret=get("TG_WEBHOOK_SECRET"),
            admin_tg_id=get_int("ADMIN_TG_ID", None),
            daily_message_limit=get_int("DAILY_MESSAGE_LIMIT", cls.daily_message_limit),
            daily_poll_limit=get_int("DAILY_POLL_LIMIT", cls.daily_poll_limit),
            digest_hour=get_int("DIGEST_HOUR", cls.digest_hour),
            tz=get("TZ") or cls.tz,
            db_path=get("DB_PATH") or cls.db_path,
            allowed_redirect_uris=redirect_uris,
            access_token_ttl=get_int("ACCESS_TOKEN_TTL", cls.access_token_ttl),
            refresh_token_ttl=get_int("REFRESH_TOKEN_TTL", cls.refresh_token_ttl),
            log_level=(get("LOG_LEVEL") or cls.log_level).upper(),
        )

        if errors:
            raise ConfigError("invalid configuration: " + "; ".join(errors))
        return settings
