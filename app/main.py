"""Starlette application factory."""

import contextlib
from collections.abc import AsyncIterator

from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route

from app.config import Settings
from app.db import open_database


async def health(request: Request) -> JSONResponse:
    return JSONResponse({"status": "ok"})


def create_app(settings: Settings) -> Starlette:
    @contextlib.asynccontextmanager
    async def lifespan(app: Starlette) -> AsyncIterator[None]:
        async with open_database(settings.db_path) as db:
            app.state.db = db
            yield

    app = Starlette(routes=[Route("/health", health, methods=["GET"])], lifespan=lifespan)
    app.state.settings = settings
    return app
