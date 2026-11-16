"""Builds the FastMCP server (Streamable HTTP, stateless per MCP spec 2026-07-28)."""

from __future__ import annotations

import contextlib
import logging

from fastmcp import FastMCP
from starlette.requests import Request
from starlette.responses import JSONResponse

from youtube_insights_mcp import __version__, youtube_api
from youtube_insights_mcp import auth as auth_lib
from youtube_insights_mcp import config as config_lib
from youtube_insights_mcp import storage as storage_lib
from youtube_insights_mcp.tools import context as context_lib
from youtube_insights_mcp.tools import owner_tools, public_tools

SERVER_NAME = "YouTube Insights"
INSTRUCTIONS = """\
Analyze YouTube channels and videos.
- Public tools (any channel): get_channel_overview, list_channel_videos, get_video_details,
  get_video_comments, analyze_channel, compare_channels, get_view_velocity, search_youtube.
- Owner tools (the signed-in channel): get_my_channel, get_my_video_performance,
  get_my_audience, get_my_retention_curve, query_my_analytics.
- Shorts "viewed vs swiped away", impressions and CTR are not in any API: ask the user for
  them from YouTube Studio and save with record_studio_metrics.
- search_youtube costs 100 quota units; prefer channel tools. Check get_quota_status.
- update_video_metadata (if enabled): always preview with confirm=false and get approval.
"""


def create_server(settings: config_lib.Settings) -> FastMCP:
    """Creates the MCP server with all tools registered."""
    logging.basicConfig(level=settings.log_level, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    store = storage_lib.Storage(settings.database_path)
    store.cache_purge_expired()
    api = youtube_api.YouTubeApi(
        settings.youtube_api_key, store, settings.daily_quota_units, settings.cache_ttl_seconds
    )
    app = context_lib.AppContext(
        settings=settings, store=store, api=api, owner_tokens=auth_lib.OwnerTokenSource(settings)
    )

    @contextlib.asynccontextmanager
    async def lifespan(_server: FastMCP):
        try:
            yield {}
        finally:
            await api.aclose()
            store.close()

    middleware = []
    if settings.auth_mode is config_lib.AuthMode.GOOGLE and settings.allowed_emails:
        middleware.append(auth_lib.EmailAllowlistMiddleware(settings.allowed_emails))

    mcp = FastMCP(
        SERVER_NAME,
        instructions=INSTRUCTIONS,
        version=__version__,
        auth=auth_lib.build_auth(settings),
        middleware=middleware,
        lifespan=lifespan,
        mask_error_details=True,  # Unexpected exceptions never leak internals to clients.
    )
    public_tools.register(mcp, app)
    owner_tools.register(mcp, app)

    @mcp.custom_route("/healthz", methods=["GET"])
    async def healthz(_request: Request) -> JSONResponse:
        return JSONResponse({"status": "ok", "version": __version__})

    return mcp


def http_app(settings: config_lib.Settings | None = None):
    """ASGI app for uvicorn / gunicorn / Cloud Run (`uvicorn ...:asgi_app --factory`)."""
    settings = settings or config_lib.load_settings()
    return create_server(settings).http_app(path=settings.mcp_path, stateless_http=True, json_response=True)
