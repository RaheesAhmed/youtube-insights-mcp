"""Authentication for MCP clients, plus resolving the Google token used for owner analytics."""

from __future__ import annotations

import hashlib
import hmac
import logging

from fastmcp.exceptions import ToolError
from fastmcp.server.auth import AccessToken, AuthProvider, TokenVerifier
from fastmcp.server.auth.providers.google import GoogleProvider
from fastmcp.server.dependencies import get_access_token
from fastmcp.server.middleware import Middleware, MiddlewareContext

from youtube_insights_mcp import config as config_lib
from youtube_insights_mcp import google_oauth

_LOG = logging.getLogger(__name__)


class BearerTokenVerifier(TokenVerifier):
    """Accepts any of the configured static bearer tokens (constant-time compare)."""

    def __init__(self, tokens: tuple[str, ...]):
        super().__init__()
        self._digests = [hashlib.sha256(t.encode()).digest() for t in tokens]

    async def verify_token(self, token: str) -> AccessToken | None:
        digest = hashlib.sha256(token.encode()).digest()
        matched = False
        for known in self._digests:  # Check all, so timing does not leak which one matched.
            matched |= hmac.compare_digest(digest, known)
        if not matched:
            return None
        return AccessToken(token=token, client_id="bearer-token", scopes=[], expires_at=None)


class EmailAllowlistMiddleware(Middleware):
    """In google mode, only Google accounts on YTMCP_ALLOWED_EMAILS may call anything."""

    def __init__(self, allowed: frozenset[str]):
        self._allowed = allowed

    async def on_request(self, context: MiddlewareContext, call_next):
        token = get_access_token()
        email = ((token.claims or {}).get("email") or "").lower() if token else ""
        if email not in self._allowed:
            _LOG.warning("Rejected request from Google account not on the allowlist.")
            raise ToolError("This Google account is not allowed to use this server.")
        return await call_next(context)


def build_auth(settings: config_lib.Settings) -> AuthProvider | None:
    """Creates the FastMCP auth provider for the configured mode."""
    if settings.auth_mode is config_lib.AuthMode.NONE:
        return None
    if settings.auth_mode is config_lib.AuthMode.TOKEN:
        return BearerTokenVerifier(settings.bearer_tokens)
    base_url = settings.public_base_url or f"http://{settings.host}:{settings.port}"
    
    return GoogleProvider(
        client_id=settings.google_client_id,
        client_secret=settings.google_client_secret,
        base_url=base_url,
        required_scopes=settings.oauth_scopes,
        jwt_signing_key=settings.jwt_signing_key,
        redirect_path="/auth/callback",
        extra_authorize_params={"access_type": "offline", "prompt": "select_account consent"},
    )


class OwnerTokenSource:
    """Finds the Google access token for owner-only APIs (Analytics, video updates)."""

    def __init__(self, settings: config_lib.Settings):
        self._settings = settings
        self._stored = google_oauth.StoredCredentials(settings) if settings.has_oauth_client else None

    async def get(self) -> str:
        if self._settings.auth_mode is config_lib.AuthMode.GOOGLE:
            token = get_access_token()
            if not token or not token.token:
                raise ToolError("Sign in with Google through your MCP client first.")
            return token.token  # The OAuth proxy hands us the user's upstream Google token.
        if not self._stored:
            raise ToolError("Owner analytics need YTMCP_GOOGLE_CLIENT_ID/SECRET, then `youtube-insights-mcp login`.")
        try:
            return await self._stored.access_token()
        except google_oauth.NotSignedInError as exc:
            raise ToolError(str(exc)) from None
