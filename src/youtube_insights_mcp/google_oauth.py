"""Stored Google OAuth credentials for the channel owner (token / none auth modes).

In ``google`` auth mode every MCP user signs in through the connector, so this file is
not used. In ``token`` / ``none`` mode, the owner runs ``youtube-insights-mcp login``
once; the refresh token is stored with 0600 permissions and refreshed automatically.
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import http.server
import json
import pathlib
import secrets
import threading
import time
import urllib.parse
from typing import Any

import httpx

from youtube_insights_mcp import config as config_lib

AUTH_URL = "https://accounts.google.com/o/oauth2/v2/auth"
TOKEN_URL = "https://oauth2.googleapis.com/token"  # noqa: S105 (URL, not a secret)
LOGIN_REDIRECT_PORT = 8765
LOGIN_REDIRECT_PATH = "/oauth2callback"
LOGIN_REDIRECT_URI = f"http://localhost:{LOGIN_REDIRECT_PORT}{LOGIN_REDIRECT_PATH}"


class NotSignedInError(RuntimeError):
    """Owner analytics need a Google sign-in first."""


class StoredCredentials:
    """Loads, refreshes and saves the owner's Google OAuth token."""

    def __init__(self, settings: config_lib.Settings):
        self._settings = settings
        self._path = settings.credentials_path
        self._lock = asyncio.Lock()
        self._data: dict[str, Any] | None = None

    @property
    def available(self) -> bool:
        return self._path.exists()

    def _load(self) -> dict[str, Any]:
        if self._data is None:
            if not self._path.exists():
                raise NotSignedInError(
                    "No Google sign-in stored. Run `uv run youtube-insights-mcp login` once, "
                    "or use YTMCP_AUTH_MODE=google so each user signs in through the connector."
                )
            self._data = json.loads(self._path.read_text())
        return self._data

    async def access_token(self) -> str:
        """Returns a valid access token, refreshing it when it is about to expire."""
        async with self._lock:
            data = self._load()
            if data.get("expires_at", 0) - 60 > time.time():
                return data["access_token"]
            async with httpx.AsyncClient(timeout=15) as client:
                resp = await client.post(
                    TOKEN_URL,
                    data={
                        "client_id": self._settings.google_client_id,
                        "client_secret": self._settings.google_client_secret,
                        "refresh_token": data["refresh_token"],
                        "grant_type": "refresh_token",
                    },
                )
            if resp.status_code != 200:
                raise NotSignedInError("Google refused the stored sign-in (revoked or expired). Run `login` again.")
            fresh = resp.json()
            data.update(access_token=fresh["access_token"], expires_at=time.time() + fresh.get("expires_in", 3600))
            save_token(self._path, data)
            return data["access_token"]


def save_token(path: pathlib.Path, data: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(data))
    tmp.chmod(0o600)
    tmp.replace(path)


def build_login_url(settings: config_lib.Settings, login_hint: str | None = None) -> tuple[str, str, str]:
    """Returns (url, state, code_verifier) for a PKCE authorization-code login.

    Args:
      settings: Server settings (client id, scopes).
      login_hint: Optional email of the channel owner, pre-selected in Google's chooser.
    """
    verifier = secrets.token_urlsafe(64)
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
    state = secrets.token_urlsafe(24)
    params = {
        "client_id": settings.google_client_id,
        "redirect_uri": LOGIN_REDIRECT_URI,
        "response_type": "code",
        "scope": " ".join(settings.oauth_scopes),
        "access_type": "offline",
        # select_account: always show Google's account chooser instead of reusing the active account.
        # consent: always return a refresh token.
        "prompt": "select_account consent",
        "include_granted_scopes": "true",
        "state": state,
        "code_challenge": challenge,
        "code_challenge_method": "S256",
    }
    if login_hint:
        params["login_hint"] = login_hint
    return f"{AUTH_URL}?{urllib.parse.urlencode(params)}", state, verifier

def exchange_code(settings: config_lib.Settings, redirected_url: str, state: str, verifier: str) -> dict[str, Any]:
    """Validates the redirect (state + issuer) and exchanges the code for tokens."""
    params = dict(urllib.parse.parse_qsl(urllib.parse.urlparse(redirected_url).query))
    if params.get("error"):
        raise NotSignedInError(f"Google sign-in failed: {params['error']}")
    if not secrets.compare_digest(params.get("state", ""), state):
        raise NotSignedInError("State mismatch: the sign-in response does not belong to this login attempt.")
    if params.get("iss") and params["iss"] != "https://accounts.google.com":
        raise NotSignedInError("Unexpected issuer in the sign-in response.")
    resp = httpx.post(
        TOKEN_URL,
        timeout=15,
        data={
            "client_id": settings.google_client_id,
            "client_secret": settings.google_client_secret,
            "code": params.get("code", ""),
            "code_verifier": verifier,
            "redirect_uri": LOGIN_REDIRECT_URI,
            "grant_type": "authorization_code",
        },
    )
    if resp.status_code != 200:
        raise NotSignedInError(f"Token exchange failed: {resp.json().get('error_description', resp.status_code)}")
    tokens = resp.json()
    if "refresh_token" not in tokens:
        raise NotSignedInError(
            "Google returned no refresh token. Remove the app at myaccount.google.com/permissions and retry."
        )
    granted = set(tokens.get("scope", "").split())
    missing = {config_lib.SCOPE_ANALYTICS_READONLY, config_lib.SCOPE_YOUTUBE_READONLY} - granted
    if missing:
        raise NotSignedInError(f"Please tick every permission box. Missing: {', '.join(sorted(missing))}")
    data = {
        "access_token": tokens["access_token"],
        "refresh_token": tokens["refresh_token"],
        "expires_at": time.time() + tokens.get("expires_in", 3600),
        "scope": tokens.get("scope"),
    }
    save_token(settings.credentials_path, data)
    return data


def wait_for_redirect(timeout_seconds: int = 300) -> str | None:
    """Runs a one-shot localhost server and returns the full redirect URL it receives."""
    received: dict[str, str] = {}

    class Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self):  # noqa: N802 (stdlib name)
            if not self.path.startswith(LOGIN_REDIRECT_PATH):
                self.send_response(404)
                self.end_headers()
                return
            received["url"] = f"http://localhost:{LOGIN_REDIRECT_PORT}{self.path}"
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.end_headers()
            self.wfile.write(b"<h2>Signed in. You can close this tab and return to the terminal.</h2>")

        def log_message(self, *_):
            pass

    server = http.server.HTTPServer(("127.0.0.1", LOGIN_REDIRECT_PORT), Handler)
    server.timeout = 1
    thread = threading.Thread(
        target=lambda: [server.handle_request() for _ in range(timeout_seconds) if "url" not in received], daemon=True
    )
    thread.start()
    deadline = time.time() + timeout_seconds
    while "url" not in received and time.time() < deadline:
        time.sleep(0.2)
    server.server_close()
    return received.get("url")
