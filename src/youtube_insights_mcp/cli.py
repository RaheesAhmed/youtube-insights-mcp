"""Command line: serve, login, doctor, gen-token."""

from __future__ import annotations

import argparse
import asyncio
import json
import secrets
import sys
import webbrowser

from youtube_insights_mcp import config as config_lib


def _cmd_serve(args: argparse.Namespace) -> int:
    import uvicorn

    from youtube_insights_mcp import server

    settings = config_lib.load_settings(args.env_file)
    print(
        f"YouTube Insights MCP on http://{settings.host}:{settings.port}{settings.mcp_path} "
        f"(auth: {settings.auth_mode.value})",
        file=sys.stderr,
    )
    uvicorn.run(
        server.http_app(settings),
        host=settings.host,
        port=settings.port,
        log_level=settings.log_level.lower(),
        proxy_headers=True,
        forwarded_allow_ips="*",
        server_header=False,
    )
    return 0


def _cmd_login(args: argparse.Namespace) -> int:
    from youtube_insights_mcp import google_oauth

    settings = config_lib.load_settings(args.env_file)
    if not settings.has_oauth_client:
        print("Set YTMCP_GOOGLE_CLIENT_SECRETS_FILE (or CLIENT_ID/SECRET) first.", file=sys.stderr)
        return 1
    url, state, verifier = google_oauth.build_login_url(settings, login_hint=args.email)
    print(
        "\n1. Make sure this redirect URI is listed on your OAuth client in Google Cloud Console:\n"
        f"   {google_oauth.LOGIN_REDIRECT_URI}\n\n2. Open this link and sign in as the channel owner:\n\n{url}\n"
    )
    if args.manual:
        redirected = input(
            "3. After approving, the browser lands on a localhost page (it may fail to load).\n"
            "   Paste that full address here: "
        ).strip()
    else:
        webbrowser.open(url)
        print("Waiting for the browser (5 minutes)...")
        redirected = google_oauth.wait_for_redirect()
        if not redirected:
            print("Timed out. Re-run with --manual to paste the redirect address instead.", file=sys.stderr)
            return 1
    try:
        google_oauth.exchange_code(settings, redirected, state, verifier)
    except google_oauth.NotSignedInError as exc:
        print(f"Login failed: {exc}", file=sys.stderr)
        return 1
    print(f"Signed in. Token saved to {settings.credentials_path} (owner-only file).")
    return 0


def _cmd_doctor(args: argparse.Namespace) -> int:
    from youtube_insights_mcp import storage, youtube_api

    try:
        settings = config_lib.load_settings(args.env_file)
    except config_lib.ConfigError as exc:
        print(f"Config error: {exc}", file=sys.stderr)
        return 1
    print(json.dumps(settings.describe(), indent=2))

    async def check() -> None:
        store = storage.Storage(settings.database_path)
        api = youtube_api.YouTubeApi(settings.youtube_api_key, store, settings.daily_quota_units, 60)
        try:
            channel_id = await api.resolve_channel_id("@YouTube", allow_search=False)
            print(f"YouTube Data API: OK (resolved @YouTube -> {channel_id})")
        except youtube_api.YouTubeApiError as exc:
            print(f"YouTube Data API: FAILED - {exc}")
        finally:
            await api.aclose()
        print(f"Owner sign-in stored: {settings.credentials_path.exists()}")

    asyncio.run(check())
    return 0


def _cmd_gen_token(_args: argparse.Namespace) -> int:
    print(secrets.token_urlsafe(40))
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="youtube-insights-mcp", description="YouTube Insights MCP server")
    parser.add_argument("--env-file", default=None, help="Path to a .env file (default ./.env)")
    sub = parser.add_subparsers(dest="command")
    sub.add_parser("serve", help="Run the MCP server (default)")
    login = sub.add_parser("login", help="Sign in once as the channel owner (token/none modes)")
    login.add_argument("--manual", action="store_true", help="Paste the redirect URL instead of a local listener")
    login.add_argument("--email", default=None, help="Google account that owns the channel (pre-selected)")
    sub.add_parser("doctor", help="Check configuration and API access")
    sub.add_parser("gen-token", help="Print a new random bearer token")
    args = parser.parse_args(argv)
    handlers = {"serve": _cmd_serve, "login": _cmd_login, "doctor": _cmd_doctor, "gen-token": _cmd_gen_token}
    try:
        return handlers[args.command or "serve"](args)
    except config_lib.ConfigError as exc:
        print(f"Config error: {exc}", file=sys.stderr)
        return 2
