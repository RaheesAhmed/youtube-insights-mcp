"""Tools that work on ANY public channel or video (YouTube Data API key only)."""

from __future__ import annotations

import asyncio
from typing import Annotated, Any, Literal

from fastmcp import FastMCP
from fastmcp.exceptions import ToolError
from pydantic import Field

from youtube_insights_mcp import analysis, youtube_api
from youtube_insights_mcp.tools import context as context_lib

READ_ONLY = {"readOnlyHint": True, "openWorldHint": True, "idempotentHint": True}

ChannelRef = Annotated[
    str, Field(description="Channel ID (UC...), @handle, channel URL, or channel name.", min_length=2, max_length=200)
]
ContentType = Literal["all", "shorts", "long"]


def _filter(videos: list[dict[str, Any]], content_type: ContentType) -> list[dict[str, Any]]:
    if content_type == "shorts":
        return [v for v in videos if v.get("is_short")]
    if content_type == "long":
        return [v for v in videos if not v.get("is_short")]
    return videos


async def load_channel_videos(
    app: context_lib.AppContext, channel: str, max_videos: int, refresh: bool = False
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    """Resolves a channel and fetches its newest `max_videos` uploads with full stats."""
    channel_id = await app.api.resolve_channel_id(channel)
    channels = await app.api.get_channels([channel_id])
    if not channels:
        raise ToolError(f"Channel not found: {channel}")
    info = channels[0]
    ids = await app.api.list_upload_ids(info["uploads_playlist_id"], max_videos)
    return info, await app.api.get_videos(ids, refresh=refresh)


def register(mcp: FastMCP, app: context_lib.AppContext) -> None:
    """Registers the public-data tools."""

    @mcp.tool(annotations=READ_ONLY)
    async def get_channel_overview(channel: ChannelRef) -> dict[str, Any]:
        """Channel profile and totals: subscribers, total views, video count, description,
        keywords, topics, country and creation date. Costs 1-2 quota units."""
        channel_id = await app.api.resolve_channel_id(channel)
        found = await app.api.get_channels([channel_id])
        if not found:
            raise ToolError(f"Channel not found: {channel}")
        return found[0]

    @mcp.tool(annotations=READ_ONLY)
    async def list_channel_videos(
        channel: ChannelRef,
        max_videos: Annotated[int, Field(ge=1, le=500)] = 50,
        content_type: ContentType = "all",
        sort_by: Literal["newest", "views", "engagement"] = "newest",
        include_descriptions: bool = False,
    ) -> dict[str, Any]:
        """Lists a channel's uploads with title, tags, views, likes, comments, duration,
        publish time and whether each is a Short. About 1 unit per 25 videos."""
        info, videos = await load_channel_videos(app, channel, max_videos)
        videos = _filter(videos, content_type)
        key = {"views": lambda v: v["views"] or 0, "engagement": lambda v: v["engagement_rate_pct"] or 0}.get(sort_by)
        if key:
            videos = sorted(videos, key=key, reverse=True)
        if not include_descriptions:
            videos = [{k: v for k, v in vid.items() if k != "description"} for vid in videos]
        return {
            "channel": {k: info[k] for k in ("channel_id", "title", "handle", "subscribers")},
            "returned": len(videos),
            "videos": videos,
        }

    @mcp.tool(annotations=READ_ONLY)
    async def get_video_details(
        videos: Annotated[list[str], Field(min_length=1, max_length=50, description="Video IDs or URLs (up to 50).")],
    ) -> list[dict[str, Any]]:
        """Full metadata and live stats for specific videos: title, description, tags,
        category, duration, views, likes, comments, engagement, Short or not. 1 unit."""
        try:
            ids = [youtube_api.extract_video_id(v) for v in videos]
        except youtube_api.YouTubeApiError as exc:
            raise ToolError(str(exc)) from None
        return await app.api.get_videos(ids)

    @mcp.tool(annotations=READ_ONLY)
    async def get_video_comments(
        video: Annotated[str, Field(description="Video ID or URL.")],
        max_results: Annotated[int, Field(ge=1, le=100)] = 30,
        order: Literal["relevance", "time"] = "relevance",
    ) -> list[dict[str, Any]]:
        """Top-level comments on a video (author, text, likes, reply count). 1 unit."""
        return await app.api.get_comments(youtube_api.extract_video_id(video), max_results, order)

    @mcp.tool(annotations=READ_ONLY)
    async def analyze_channel(
        channel: ChannelRef,
        max_videos: Annotated[int, Field(ge=5, le=500)] = 100,
        content_type: ContentType = "all",
    ) -> dict[str, Any]:
        """Performance report for any channel: Shorts vs long-form, upload cadence, best
        publish hour/weekday (UTC), title words that get the most views, top tags,
        top/bottom videos and 3x-median outliers."""
        info, videos = await load_channel_videos(app, channel, max_videos)
        report = analysis.channel_insights(_filter(videos, content_type))
        return {
            "channel": {
                k: info[k] for k in ("channel_id", "title", "handle", "subscribers", "total_views", "video_count")
            },
            "analyzed_videos": len(videos),
            **report,
        }

    @mcp.tool(annotations=READ_ONLY)
    async def compare_channels(
        channels: Annotated[
            list[str], Field(min_length=2, max_length=6, description="2-6 channel IDs, @handles or URLs.")
        ],
        max_videos_each: Annotated[int, Field(ge=5, le=200)] = 50,
    ) -> list[dict[str, Any]]:
        """Side-by-side comparison of channels: subscribers, cadence, median views,
        Shorts share, engagement and each channel's best-performing video."""

        async def one(ref: str) -> dict[str, Any]:
            info, videos = await load_channel_videos(app, ref, max_videos_each)
            rep = analysis.channel_insights(videos, top_n=1)
            return {
                "channel": info["title"],
                "handle": info["handle"],
                "subscribers": info["subscribers"],
                "total_views": info["total_views"],
                "video_count": info["video_count"],
                "recent_videos_analyzed": len(videos),
                "shorts_share_pct": round(100 * sum(1 for v in videos if v.get("is_short")) / max(len(videos), 1), 1),
                "overall": rep.get("overall"),
                "shorts": rep.get("shorts"),
                "long_form": rep.get("long_form"),
                "uploads_per_week": (rep.get("cadence") or {}).get("uploads_per_week"),
                "best_video": (rep.get("top_videos") or [None])[0],
            }

        return list(await asyncio.gather(*(one(c) for c in channels)))

    @mcp.tool(annotations=READ_ONLY)
    async def get_view_velocity(
        channel: ChannelRef,
        max_videos: Annotated[int, Field(ge=1, le=200)] = 30,
    ) -> dict[str, Any]:
        """Views per hour for a channel's recent videos, measured between the last two
        times this server fetched them (call it again later to see momentum)."""
        _, videos = await load_channel_videos(app, channel, max_videos, refresh=True)
        rows = analysis.view_velocity(videos, app.store.snapshots([v["video_id"] for v in videos]))
        if not rows:
            return {"note": "First snapshot stored. Call this again in 15+ minutes to measure velocity."}
        return {"videos": rows}

    @mcp.tool(annotations=READ_ONLY)
    async def search_youtube(
        query: Annotated[str, Field(min_length=1, max_length=200)],
        result_type: Literal["video", "channel"] = "video",
        max_results: Annotated[int, Field(ge=1, le=50)] = 20,
        order: Literal["relevance", "viewCount", "date", "rating"] = "relevance",
        published_after: Annotated[str | None, Field(description="RFC3339, e.g. 2026-11-01T00:00:00Z")] = None,
        video_duration: Literal["any", "short", "medium", "long"] = "any",
        region_code: Annotated[str | None, Field(pattern=r"^[A-Z]{2}$")] = None,
    ) -> dict[str, Any]:
        """Searches YouTube for videos or channels (niche research). EXPENSIVE: 100 quota
        units per call, so prefer channel tools when you know the channel. Video results
        include full stats."""
        params: dict[str, Any] = {
            "part": "snippet",
            "q": query,
            "type": result_type,
            "maxResults": max_results,
            "order": order,
        }
        if published_after:
            params["publishedAfter"] = published_after
        if region_code:
            params["regionCode"] = region_code
        if result_type == "video" and video_duration != "any":
            params["videoDuration"] = video_duration
        found = await app.api.search(params)
        items = found.get("items", [])
        if result_type == "channel":
            ids = [it["snippet"]["channelId"] for it in items]
            return {"channels": await app.api.get_channels(ids) if ids else []}
        ids = [it["id"]["videoId"] for it in items if it.get("id", {}).get("videoId")]
        videos = await app.api.get_videos(ids) if ids else []
        return {"videos": [{k: v for k, v in vid.items() if k != "description"} for vid in videos]}

    @mcp.tool(annotations=READ_ONLY)
    async def get_quota_status() -> dict[str, Any]:
        """Today's YouTube Data API quota used by this server and what is left."""
        used = app.store.quota_used_today()
        return {
            "used_units": used,
            "daily_budget": app.settings.daily_quota_units,
            "remaining_units": max(app.settings.daily_quota_units - used, 0),
            "resets": "midnight Pacific time",
            "costs": youtube_api.QUOTA_COST,
        }
