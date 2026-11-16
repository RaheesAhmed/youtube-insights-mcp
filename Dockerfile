# syntax=docker/dockerfile:1
FROM ghcr.io/astral-sh/uv:python3.12-bookworm-slim AS build
WORKDIR /app
ENV UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy
COPY pyproject.toml uv.lock README.md ./
RUN uv sync --locked --no-dev --no-install-project
COPY src ./src
RUN uv sync --locked --no-dev

FROM python:3.12-slim-bookworm
RUN useradd --create-home --uid 10001 app
WORKDIR /app
COPY --from=build --chown=app:app /app /app
ENV PATH="/app/.venv/bin:$PATH" YTMCP_HOST=0.0.0.0 YTMCP_PORT=8080 YTMCP_DATA_DIR=/app/data PYTHONUNBUFFERED=1
RUN mkdir -p /app/data && chown app:app /app/data
USER app
EXPOSE 8080
HEALTHCHECK --interval=30s --timeout=5s CMD python -c "import urllib.request,os;urllib.request.urlopen(f'http://127.0.0.1:{os.environ[\"YTMCP_PORT\"]}/healthz')"
CMD ["youtube-insights-mcp", "serve"]
