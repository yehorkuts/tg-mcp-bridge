# tg-mcp-bridge

A remote MCP server that lets Claude send messages and practice quizzes to a user in Telegram. Users sign in with the Telegram Login Widget; the server implements OAuth 2.1 + DCR itself.

- What we are building: [SPEC.md](SPEC.md)
- Implementation order and status: [PLAN.md](PLAN.md)
- Process rules: [CLAUDE.md](CLAUDE.md)

## Development

Requires [uv](https://docs.astral.sh/uv/) and Docker. Python 3.14 is pinned in `.python-version`.

```sh
uv sync                 # create .venv from uv.lock
uv run pytest           # run tests
```

Run the app in Docker with placeholder config:

```sh
export ENV_FILE=.env.example   # every compose command needs it locally, not only `up`
docker compose up -d --build
docker compose exec app curl -s localhost:8000/health   # {"status":"ok"}
docker compose down
```

The app refuses to start if `BASE_URL` or `BOT_TOKEN` is missing.

## Configuration

All settings come from environment variables; see `.env.example` and SPEC.md, "Configuration". The real env file exists only on the production server and is never committed.
