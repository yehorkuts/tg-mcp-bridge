"""Entry point: `python -m app`. Validates config before starting the server."""

import logging
import sys

import uvicorn

from app.config import ConfigError, Settings
from app.main import create_app


def main() -> None:
    try:
        settings = Settings.from_env()
    except ConfigError as exc:
        print(f"Startup failed: {exc}", file=sys.stderr)
        sys.exit(1)

    logging.basicConfig(level=settings.log_level)
    uvicorn.run(
        create_app(settings),
        host="0.0.0.0",
        port=8000,
        log_level=settings.log_level.lower(),
        proxy_headers=True,
    )


if __name__ == "__main__":
    main()
