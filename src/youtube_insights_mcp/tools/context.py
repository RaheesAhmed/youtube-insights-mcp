"""Shared objects passed to every tool module."""

from __future__ import annotations

import dataclasses

from youtube_insights_mcp import auth, config, storage, youtube_api


@dataclasses.dataclass
class AppContext:
    settings: config.Settings
    store: storage.Storage
    api: youtube_api.YouTubeApi
    owner_tokens: auth.OwnerTokenSource
