"""Runtime configuration loaded from environment variables (and an optional .env file).

Every setting is prefixed with ``YTMCP_``. See ``.env.example`` for the full list.
"""

from __future__ import annotations

import dataclasses
import enum
import json
import os
import pathlib

from dotenv import load_dotenv

_ENV_PREFIX = "YTMCP_"

# OAuth scopes. Read-only by default; write scope only when write tools are enabled.
SCOPE_OPENID = "openid"
SCOPE_EMAIL = "https://www.googleapis.com/auth/userinfo.email"
SCOPE_YOUTUBE_READONLY = "https://www.googleapis.com/auth/youtube.readonly"
SCOPE_ANALYTICS_READONLY = "https://www.googleapis.com/auth/yt-analytics.readonly"
SCOPE_YOUTUBE_WRITE = "https://www.googleapis.com/auth/youtube.force-ssl"


class AuthMode(enum.StrEnum):
    """How MCP clients authenticate to this server."""

    GOOGLE = "google"  # OAuth 2.1 via Google (remote / Claude.ai connectors).
    TOKEN = "token"  # noqa: S105 (mode name, not a secret)  # Static bearer token(s) (Claude Code, scripts, private hosts).
    NONE = "none"  # No auth. Only allowed when bound to localhost.


class ConfigError(ValueError):
    """Raised when the configuration is missing or unsafe."""


@dataclasses.dataclass(frozen=True)
class Settings:
    """Validated server settings."""

    youtube_api_key: str
    google_client_id: str
    google_client_secret: str
    auth_mode: AuthMode
    bearer_tokens: tuple[str, ...]
    allowed_emails: frozenset[str]
    public_base_url: str
    jwt_signing_key: str
    host: str
    port: int
    mcp_path: str
    data_dir: pathlib.Path
    daily_quota_units: int
    cache_ttl_seconds: int
    enable_write_tools: bool
    log_level: str

    @property
    def database_path(self) -> pathlib.Path:
        return self.data_dir / "youtube_insights.sqlite3"

    @property
    def credentials_path(self) -> pathlib.Path:
        return self.data_dir / "credentials" / "google_oauth_token.json"

    @property
    def has_oauth_client(self) -> bool:
        return bool(self.google_client_id and self.google_client_secret)

    @property
    def oauth_scopes(self) -> list[str]:
        scopes = [SCOPE_OPENID, SCOPE_EMAIL, SCOPE_YOUTUBE_READONLY, SCOPE_ANALYTICS_READONLY]
        if self.enable_write_tools:
            scopes.append(SCOPE_YOUTUBE_WRITE)
        return scopes

    def describe(self) -> dict[str, object]:
        """Returns a secret-free summary for logs and the `doctor` command."""
        return {
            "auth_mode": self.auth_mode.value,
            "listen": f"http://{self.host}:{self.port}{self.mcp_path}",
            "public_base_url": self.public_base_url or None,
            "youtube_api_key": "set" if self.youtube_api_key else "MISSING",
            "google_oauth_client": "set" if self.has_oauth_client else "not set",
            "bearer_tokens": len(self.bearer_tokens),
            "allowed_emails": sorted(self.allowed_emails) or "anyone with a Google account",
            "data_dir": str(self.data_dir),
            "daily_quota_units": self.daily_quota_units,
            "write_tools": self.enable_write_tools,
        }


def _env(name: str, default: str = "") -> str:
    return os.environ.get(_ENV_PREFIX + name, default).strip()


def _env_bool(name: str, default: bool = False) -> bool:
    raw = _env(name, "true" if default else "false").lower()
    return raw in {"1", "true", "yes", "on"}


def _split(raw: str) -> list[str]:
    return [item.strip() for item in raw.split(",") if item.strip()]


def _read_client_secrets_file(path: str) -> tuple[str, str]:
    """Reads a Google OAuth client JSON ("web" or "installed" type)."""
    data = json.loads(pathlib.Path(path).expanduser().read_text())
    section = data.get("web") or data.get("installed") or {}
    return section.get("client_id", ""), section.get("client_secret", "")


def _is_local_host(host: str) -> bool:
    return host in {"127.0.0.1", "localhost", "::1"}


def load_settings(env_file: str | None = None) -> Settings:
    """Loads and validates settings.

    Args:
      env_file: Optional path to a .env file. Defaults to ``./.env`` if present.

    Returns:
      The validated settings.

    Raises:
      ConfigError: If a required value is missing or the combination is unsafe.
    """
    load_dotenv(env_file or ".env", override=False)

    client_id = _env("GOOGLE_CLIENT_ID")
    client_secret = _env("GOOGLE_CLIENT_SECRET")
    secrets_file = _env("GOOGLE_CLIENT_SECRETS_FILE")
    if secrets_file and not (client_id and client_secret):
        client_id, client_secret = _read_client_secrets_file(secrets_file)

    try:
        auth_mode = AuthMode(_env("AUTH_MODE", "token").lower())
    except ValueError as exc:
        raise ConfigError("YTMCP_AUTH_MODE must be one of: google, token, none") from exc

    host = _env("HOST", "127.0.0.1")
    mcp_path = "/" + _env("MCP_PATH", "/mcp").strip("/")
    settings = Settings(
        youtube_api_key=_env("YOUTUBE_API_KEY"),
        google_client_id=client_id,
        google_client_secret=client_secret,
        auth_mode=auth_mode,
        bearer_tokens=tuple(_split(_env("BEARER_TOKENS"))),
        allowed_emails=frozenset(e.lower() for e in _split(_env("ALLOWED_EMAILS"))),
        public_base_url=_env("PUBLIC_BASE_URL").rstrip("/"),
        jwt_signing_key=_env("JWT_SIGNING_KEY"),
        host=host,
        port=int(_env("PORT", os.environ.get("PORT", "8080"))),
        mcp_path=mcp_path,
        data_dir=pathlib.Path(_env("DATA_DIR", "./data")).expanduser().resolve(),
        daily_quota_units=int(_env("DAILY_QUOTA_UNITS", "10000")),
        cache_ttl_seconds=int(_env("CACHE_TTL_SECONDS", "3600")),
        enable_write_tools=_env_bool("ENABLE_WRITE_TOOLS"),
        log_level=_env("LOG_LEVEL", "INFO").upper(),
    )
    _validate(settings)
    return settings


def _validate(settings: Settings) -> None:
    if not settings.youtube_api_key:
        raise ConfigError("YTMCP_YOUTUBE_API_KEY is required (Google Cloud API key).")
    if settings.auth_mode is AuthMode.NONE and not _is_local_host(settings.host):
        raise ConfigError(
            "YTMCP_AUTH_MODE=none is only allowed with YTMCP_HOST=127.0.0.1. "
            "Use 'token' or 'google' for anything reachable from the network."
        )
    if settings.auth_mode is AuthMode.TOKEN:
        if not settings.bearer_tokens:
            raise ConfigError(
                "YTMCP_BEARER_TOKENS is required in token mode. "
                "Generate one with: uv run youtube-insights-mcp gen-token"
            )
        if any(len(t) < 32 for t in settings.bearer_tokens):
            raise ConfigError("Every bearer token must be at least 32 characters long.")
    if settings.auth_mode is AuthMode.GOOGLE:
        if not settings.has_oauth_client:
            raise ConfigError("Google mode needs YTMCP_GOOGLE_CLIENT_ID/SECRET or a client secrets file.")
        if not settings.public_base_url.startswith("https://") and not _is_local_host(settings.host):
            raise ConfigError("Google mode needs YTMCP_PUBLIC_BASE_URL with https:// (your public URL).")
        if len(settings.jwt_signing_key) < 32:
            raise ConfigError("Google mode needs YTMCP_JWT_SIGNING_KEY (32+ random characters).")
