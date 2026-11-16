"""Tools for YOUR OWN channel: YouTube Analytics (needs Google sign-in), Studio-only
metrics you paste in, and opt-in metadata editing."""

from __future__ import annotations

import datetime
import re
from typing import Annotated, Any, Literal

from fastmcp import FastMCP
from fastmcp.exceptions import ToolError
from pydantic import Field

from youtube_insights_mcp import analysis, youtube_api
from youtube_insights_mcp.tools import context as context_lib

READ_ONLY = {"readOnlyHint": True, "openWorldHint": True, "idempotentHint": True}
_SAFE_PARAM_RE = re.compile(r"^[\w,;=:.\-]*$")
_CONTENT_TYPE_FILTER = {"shorts": "SHORTS", "long": "VIDEO_ON_DEMAND", "live": "LIVE_STREAM"}
_VIDEO_METRICS = (
    "views,engagedViews,estimatedMinutesWatched,averageViewDuration,averageViewPercentage,"
    "likes,comments,shares,subscribersGained,subscribersLost"
)
_BREAKDOWNS = {
    "country": (
        "country",
        "views,engagedViews,estimatedMinutesWatched,averageViewPercentage,subscribersGained",
        "-views",
    ),
    "age_gender": ("ageGroup,gender", "viewerPercentage", None),
    "traffic_source": ("insightTrafficSourceType", "views,engagedViews,estimatedMinutesWatched", "-views"),
    "device": ("deviceType", "views,estimatedMinutesWatched", "-views"),
    "subscribed_status": ("subscribedStatus", "views,engagedViews,averageViewPercentage", None),
    "content_type": (
        "creatorContentType",
        "views,engagedViews,estimatedMinutesWatched,averageViewPercentage,likes,shares,subscribersGained",
        None,
    ),
    "day": ("day", "views,engagedViews,estimatedMinutesWatched,subscribersGained,subscribersLost", "day"),
}

DateStr = Annotated[str | None, Field(pattern=r"^\d{4}-\d{2}-\d{2}$", description="YYYY-MM-DD")]
ContentType = Literal["all", "shorts", "long", "live"]


def _dates(start: str | None, end: str | None, default_days: int = 28) -> tuple[str, str]:
    today = datetime.date.today()
    end_date = end or (today - datetime.timedelta(days=1)).isoformat()
    start_date = (
        start or (datetime.date.fromisoformat(end_date) - datetime.timedelta(days=default_days - 1)).isoformat()
    )
    if start_date > end_date:
        raise ToolError("start_date must be on or before end_date.")
    return start_date, end_date


def _filters(content_type: ContentType, extra: str | None = None) -> str | None:
    parts = [f"creatorContentType=={_CONTENT_TYPE_FILTER[content_type]}"] if content_type != "all" else []
    if extra:
        parts.append(extra)
    return ";".join(parts) or None


def register(mcp: FastMCP, app: context_lib.AppContext) -> None:
    """Registers owner-only tools."""

    async def report(params: dict[str, Any]) -> list[dict[str, Any]]:
        bearer = await app.owner_tokens.get()
        query = {"ids": "channel==MINE", **{k: v for k, v in params.items() if v not in (None, "")}}
        try:
            return analysis.rows_to_dicts(await app.api.analytics_report(query, bearer))
        except youtube_api.YouTubeApiError as exc:
            raise ToolError(str(exc)) from None

    @mcp.tool(annotations=READ_ONLY)
    async def get_my_channel() -> dict[str, Any]:
        """Your own channel (the signed-in Google account): profile and totals."""
        bearer = await app.owner_tokens.get()
        channels = await app.api.get_channels([], bearer=bearer)
        if not channels:
            raise ToolError("The signed-in Google account has no YouTube channel.")
        return channels[0]

    @mcp.tool(annotations=READ_ONLY)
    async def get_my_video_performance(
        start_date: DateStr = None,
        end_date: DateStr = None,
        content_type: ContentType = "all",
        sort_by: Literal[
            "views", "engagedViews", "averageViewPercentage", "subscribersGained", "estimatedMinutesWatched", "shares"
        ] = "views",
        max_results: Annotated[int, Field(ge=1, le=200)] = 50,
    ) -> dict[str, Any]:
        """Per-video analytics for your channel: views, engaged views, watch time,
        average view duration, average % viewed (retention), likes, comments, shares
        and subscribers gained/lost. Default range: last 28 days."""
        start, end = _dates(start_date, end_date)
        rows = await report(
            {
                "startDate": start,
                "endDate": end,
                "dimensions": "video",
                "metrics": _VIDEO_METRICS,
                "filters": _filters(content_type),
                "sort": f"-{sort_by}",
                "maxResults": max_results,
            }
        )
        if rows:
            meta = {v["video_id"]: v for v in await app.api.get_videos([r["video"] for r in rows], detect_shorts=False)}
            for row in rows:
                m = meta.get(row["video"], {})
                row.update(
                    title=m.get("title"), published_at=m.get("published_at"), duration_seconds=m.get("duration_seconds")
                )
        return {"start_date": start, "end_date": end, "content_type": content_type, "videos": rows}

    @mcp.tool(annotations=READ_ONLY)
    async def get_my_audience(
        breakdown: Literal[
            "country", "age_gender", "traffic_source", "device", "subscribed_status", "content_type", "day"
        ] = "country",
        start_date: DateStr = None,
        end_date: DateStr = None,
        content_type: ContentType = "all",
        video: Annotated[str | None, Field(description="Optional video ID/URL to limit to one video.")] = None,
    ) -> dict[str, Any]:
        """Who watches and how they find you: by country, age & gender, traffic source
        (Shorts feed, search, browse, external...), device, subscribed vs not, content
        type, or day. Optionally for one video."""
        start, end = _dates(start_date, end_date)
        dims, metrics, sort = _BREAKDOWNS[breakdown]
        extra = f"video=={youtube_api.extract_video_id(video)}" if video else None
        ctype = "all" if breakdown == "content_type" else content_type
        rows = await report(
            {
                "startDate": start,
                "endDate": end,
                "dimensions": dims,
                "metrics": metrics,
                "filters": _filters(ctype, extra),
                "sort": sort,
                "maxResults": 50 if breakdown == "country" else None,
            }
        )
        return {"breakdown": breakdown, "start_date": start, "end_date": end, "rows": rows}

    @mcp.tool(annotations=READ_ONLY)
    async def get_my_retention_curve(
        video: Annotated[str, Field(description="Video ID or URL from your channel.")],
        start_date: DateStr = None,
        end_date: DateStr = None,
    ) -> dict[str, Any]:
        """Audience retention for one of your videos: share of viewers still watching at
        each point (100 points), plus how it compares with similar videos on YouTube.
        Shows exactly where people drop off."""
        video_id = youtube_api.extract_video_id(video)
        start, end = _dates(start_date, end_date, default_days=90)
        rows = await report(
            {
                "startDate": start,
                "endDate": end,
                "dimensions": "elapsedVideoTimeRatio",
                "metrics": "audienceWatchRatio,relativeRetentionPerformance",
                "filters": f"video=={video_id};audienceType==ORGANIC",
            }
        )
        drop = None
        if len(rows) > 1:
            steepest = max(
                range(1, len(rows)), key=lambda i: rows[i - 1]["audienceWatchRatio"] - rows[i]["audienceWatchRatio"]
            )
            drop = {
                "at_ratio": rows[steepest]["elapsedVideoTimeRatio"],
                "lost": round(rows[steepest - 1]["audienceWatchRatio"] - rows[steepest]["audienceWatchRatio"], 3),
            }
        return {"video_id": video_id, "start_date": start, "end_date": end, "steepest_drop": drop, "curve": rows}

    @mcp.tool(annotations=READ_ONLY)
    async def query_my_analytics(
        metrics: Annotated[str, Field(description="Comma-separated, e.g. 'views,engagedViews,averageViewPercentage'")],
        dimensions: Annotated[
            str | None, Field(description="e.g. 'day', 'video', 'country,creatorContentType'")
        ] = None,
        filters: Annotated[str | None, Field(description="e.g. 'creatorContentType==SHORTS;country==US'")] = None,
        sort: str | None = None,
        start_date: DateStr = None,
        end_date: DateStr = None,
        max_results: Annotated[int | None, Field(ge=1, le=200)] = None,
    ) -> dict[str, Any]:
        """Raw YouTube Analytics API query for your channel (advanced). See
        developers.google.com/youtube/analytics/metrics for names."""
        for name, value in (("metrics", metrics), ("dimensions", dimensions), ("filters", filters), ("sort", sort)):
            if value and not _SAFE_PARAM_RE.fullmatch(value):
                raise ToolError(f"Invalid characters in {name}.")
        start, end = _dates(start_date, end_date)
        rows = await report(
            {
                "startDate": start,
                "endDate": end,
                "metrics": metrics,
                "dimensions": dimensions,
                "filters": filters,
                "sort": sort,
                "maxResults": max_results,
            }
        )
        return {"start_date": start, "end_date": end, "rows": rows}

    # ---- Studio-only numbers (not exposed by any API) ----------------------
    @mcp.tool(annotations={"readOnlyHint": False, "destructiveHint": False, "idempotentHint": False})
    async def record_studio_metrics(
        video: Annotated[str, Field(description="Video ID or URL.")],
        viewed_pct: Annotated[float | None, Field(ge=0, le=100, description="Shorts 'Viewed' %")] = None,
        swiped_pct: Annotated[float | None, Field(ge=0, le=100, description="Shorts 'Swiped away' %")] = None,
        impressions: Annotated[int | None, Field(ge=0)] = None,
        ctr_pct: Annotated[float | None, Field(ge=0, le=100, description="Impressions click-through rate %")] = None,
        notes: Annotated[str | None, Field(max_length=1000)] = None,
    ) -> dict[str, Any]:
        """Saves numbers that only YouTube Studio shows (Shorts viewed vs swiped away,
        impressions, CTR) so they can be analyzed alongside API data."""
        video_id = youtube_api.extract_video_id(video)
        if viewed_pct is not None and swiped_pct is None:
            swiped_pct = round(100 - viewed_pct, 2)
        row_id = app.store.add_studio_metrics(
            video_id,
            viewed_pct=viewed_pct,
            swiped_pct=swiped_pct,
            impressions=impressions,
            ctr_pct=ctr_pct,
            notes=notes,
        )
        return {"saved": True, "id": row_id, "video_id": video_id}

    @mcp.tool(annotations=READ_ONLY)
    async def get_studio_metrics(
        video: Annotated[str | None, Field(description="Optional video ID/URL; omit for all.")] = None,
    ) -> list[dict[str, Any]]:
        """Studio-only numbers saved earlier with record_studio_metrics, newest first."""
        return app.store.studio_metrics(youtube_api.extract_video_id(video) if video else None)

    if not app.settings.enable_write_tools:
        return

    @mcp.tool(annotations={"readOnlyHint": False, "destructiveHint": True, "idempotentHint": True})
    async def update_video_metadata(
        video: Annotated[str, Field(description="Video ID or URL on your channel.")],
        title: Annotated[str | None, Field(max_length=100)] = None,
        description: Annotated[str | None, Field(max_length=5000)] = None,
        tags: Annotated[list[str] | None, Field(max_length=60)] = None,
        confirm: Annotated[bool, Field(description="false = preview only; true = apply.")] = False,
    ) -> dict[str, Any]:
        """Changes a video's title, description and/or tags. Always call with
        confirm=false first, show the preview to the user, and only apply with
        confirm=true after they approve. Costs 50 quota units."""
        if title is None and description is None and tags is None:
            raise ToolError("Nothing to change.")
        if tags is not None and sum(len(t) + 2 for t in tags) > 500:
            raise ToolError("Tags exceed YouTube's 500-character limit.")
        video_id = youtube_api.extract_video_id(video)
        bearer = await app.owner_tokens.get()
        current = await app.api.data_get("videos.list", {"part": "snippet", "id": video_id}, bearer=bearer)
        if not current.get("items"):
            raise ToolError("Video not found on the signed-in channel.")
        snippet = current["items"][0]["snippet"]
        changes = {
            k: {"from": snippet.get(k), "to": v}
            for k, v in (("title", title), ("description", description), ("tags", tags))
            if v is not None
        }
        if not confirm:
            return {
                "preview": True,
                "video_id": video_id,
                "changes": changes,
                "next": "Ask the user to approve, then call again with confirm=true.",
            }
        new_snippet = {
            "title": title or snippet["title"],
            "categoryId": snippet["categoryId"],
            "description": snippet.get("description", "") if description is None else description,
            "tags": snippet.get("tags", []) if tags is None else tags,
        }
        for key in ("defaultLanguage", "defaultAudioLanguage"):
            if key in snippet:
                new_snippet[key] = snippet[key]
        await app.api.update_video({"id": video_id, "snippet": new_snippet}, bearer)
        return {"updated": True, "video_id": video_id, "changes": changes}
