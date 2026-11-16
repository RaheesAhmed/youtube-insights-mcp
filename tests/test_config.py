import pytest

from youtube_insights_mcp import config


def _load(tmp_path):
    return config.load_settings(env_file=str(tmp_path / "missing.env"))


def test_none_mode_refuses_public_host(settings, monkeypatch, tmp_path):
    monkeypatch.setenv("YTMCP_HOST", "0.0.0.0")  # noqa: S104
    with pytest.raises(config.ConfigError, match="only allowed"):
        _load(tmp_path)


def test_token_mode_requires_long_token(settings, monkeypatch, tmp_path):
    monkeypatch.setenv("YTMCP_AUTH_MODE", "token")
    monkeypatch.setenv("YTMCP_BEARER_TOKENS", "short")
    with pytest.raises(config.ConfigError, match="32 characters"):
        _load(tmp_path)


def test_google_mode_requires_https_and_key(settings, monkeypatch, tmp_path):
    monkeypatch.setenv("YTMCP_AUTH_MODE", "google")
    monkeypatch.setenv("YTMCP_HOST", "0.0.0.0")  # noqa: S104
    monkeypatch.setenv("YTMCP_GOOGLE_CLIENT_ID", "id")
    monkeypatch.setenv("YTMCP_GOOGLE_CLIENT_SECRET", "secret")
    monkeypatch.setenv("YTMCP_PUBLIC_BASE_URL", "http://insecure.example")
    with pytest.raises(config.ConfigError, match="https"):
        _load(tmp_path)


def test_describe_has_no_secrets(settings):
    assert "test-key" not in str(settings.describe())
