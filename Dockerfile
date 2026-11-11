FROM python:3.14-slim

COPY --from=ghcr.io/astral-sh/uv:0.11.18 /uv /uvx /bin/

# curl is used by the compose healthcheck and for manual checks inside the container.
RUN apt-get update \
    && apt-get install -y --no-install-recommends curl \
    && rm -rf /var/lib/apt/lists/*

ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=0 \
    UV_PROJECT_ENVIRONMENT=/app/.venv \
    PATH="/app/.venv/bin:$PATH" \
    PYTHONUNBUFFERED=1

WORKDIR /app

COPY pyproject.toml uv.lock .python-version ./
RUN uv sync --frozen --no-dev --no-install-project

COPY app ./app

RUN useradd --system --uid 10001 --no-create-home appuser \
    && mkdir -p /data \
    && chown appuser /data

USER appuser

EXPOSE 8000

CMD ["python", "-m", "app"]
