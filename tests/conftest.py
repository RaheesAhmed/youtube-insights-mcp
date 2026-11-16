import pytest

from youtube_insights_mcp import config


@pytest.fixture
def settings(tmp_path, monkeypatch):
    for key in list(__import__("os").environ):
        if key.startswith("YTMCP_"):
            monkeypatch.delenv(key)
    monkeypatch.setenv("YTMCP_YOUTUBE_API_KEY", "test-key")
    monkeypatch.setenv("YTMCP_AUTH_MODE", "none")
    monkeypatch.setenv("YTMCP_DATA_DIR", str(tmp_path))
    return config.load_settings(env_file=str(tmp_path / "missing.env"))
