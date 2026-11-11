"""Starlette application factory."""

from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route

from app.config import Settings


async def health(request: Request) -> JSONResponse:
    return JSONResponse({"status": "ok"})


def create_app(settings: Settings) -> Starlette:
    app = Starlette(routes=[Route("/health", health, methods=["GET"])])
    app.state.settings = settings
    return app
