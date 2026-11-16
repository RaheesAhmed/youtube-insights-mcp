"""Async client for the YouTube Data API v3 and YouTube Analytics API v2.

* One pooled HTTP/2 connection for everything (fast).
* Quota is counted per call in SQLite and enforced before the call is made.
* Responses are cached in SQLite, so repeated questions cost no quota.
* Errors are re-raised as YouTubeApiError with secrets stripped.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import re
from typing import Any

import httpx

from youtube_insights_mcp import storage as storage_lib

DATA_API_URL = "https://www.googleapis.com/youtube/v3"
ANALYTICS_API_URL = "https://youtubeanalytics.googleapis.com/v2/reports"
SHORTS_URL = "https://www.youtube.com/shorts/{video_id}"

# Official quota costs (units) for the methods we use.
QUOTA_COST = {
    "channels.list": 1,
    "playlistItems.list": 1,
    "videos.list": 1,
    "commentThreads.list": 1,
    "search.list": 100,
    "videos.update": 50,
    "analytics": 0,  # Analytics API has its own, separate quota.
}

_RETRY_STATUS = {429, 500, 502, 503, 504}
_MAX_RETRIES = 3
_SHORTS_MAX_SECONDS = 180
_CHANNEL_ID_RE = re.compile(r"^UC[\w-]{22}$")
_VIDEO_ID_RE = re.compile(r"^[\w-]{11}$")
_HANDLE_RE = re.compile(r"^@?[\w.\-]{3,30}$")
_DURATION_RE = re.compile(r"P(?:(\d+)D)?T?(?:(\d+)H)?(?:(\d+)M)?(?:(\d+)S)?")


class YouTubeApiError(RuntimeError):
    """A YouTube API call failed. The message never contains keys or tokens."""


class QuotaExceededError(YouTubeApiError):
    """The local daily quota budget would be exceeded."""


def parse_duration(iso: str) -> int:
    """Converts an ISO-8601 duration (e.g. 'PT1M5S') to seconds."""
    match = _DURATION_RE.fullmatch(iso or "")
    if not match:
        return 0
    days, hours, minutes, seconds = (int(x) if x else 0 for x in match.groups())
    return days * 86400 + hours * 3600 + minutes * 60 + seconds


def extract_video_id(value: str) -> str:
    """Accepts a video ID or any YouTube video URL and returns the 11-char ID."""
    value = value.strip()
    if _VIDEO_ID_RE.fullmatch(value):
        return value
    match = re.search(r"(?:v=|youtu\.be/|shorts/|embed/|live/)([\w-]{11})", value)
    if not match:
        raise YouTubeApiError(f"Not a valid YouTube video ID or URL: {value[:80]!r}")
    return match.group(1)


def _int(value: Any) -> int | None:
    return int(value) if value not in (None, "") else None


def normalize_video(item: dict[str, Any]) -> dict[str, Any]:
    """Flattens a videos.list item into a compact record."""
    snippet = item.get("snippet", {})
    stats = item.get("statistics", {})
    details = item.get("contentDetails", {})
    status = item.get("status", {})
    views, likes, comments = _int(stats.get("viewCount")), _int(stats.get("likeCount")), _int(stats.get("commentCount"))
    duration = parse_duration(details.get("duration", ""))
    thumbs = snippet.get("thumbnails", {})
    best_thumb = next(
        (thumbs[k]["url"] for k in ("maxres", "standard", "high", "medium", "default") if k in thumbs), None
    )
    return {
        "video_id": item["id"],
        "channel_id": snippet.get("channelId", ""),
        "channel_title": snippet.get("channelTitle"),
        "title": snippet.get("title"),
        "description": snippet.get("description"),
        "tags": snippet.get("tags", []),
        "category_id": snippet.get("categoryId"),
        "published_at": snippet.get("publishedAt"),
        "default_language": snippet.get("defaultLanguage") or snippet.get("defaultAudioLanguage"),
        "duration_seconds": duration,
        "definition": details.get("definition"),
        "caption": details.get("caption") == "true",
        "made_for_kids": status.get("madeForKids"),
        "privacy_status": status.get("privacyStatus"),
        "views": views,
        "likes": likes,
        "comments": comments,
        "engagement_rate_pct": round(100 * ((likes or 0) + (comments or 0)) / views, 2) if views else None,
        "thumbnail_url": best_thumb,
        "url": f"https://www.youtube.com/watch?v={item['id']}",
    }


def normalize_channel(item: dict[str, Any]) -> dict[str, Any]:
    snippet, stats = item.get("snippet", {}), item.get("statistics", {})
    branding = item.get("brandingSettings", {}).get("channel", {})
    topics = item.get("topicDetails", {}).get("topicCategories", [])
    return {
        "channel_id": item["id"],
        "title": snippet.get("title"),
        "handle": snippet.get("customUrl"),
        "description": snippet.get("description"),
        "country": snippet.get("country"),
        "published_at": snippet.get("publishedAt"),
        "subscribers": _int(stats.get("subscriberCount")),
        "subscribers_hidden": stats.get("hiddenSubscriberCount", False),
        "total_views": _int(stats.get("viewCount")),
        "video_count": _int(stats.get("videoCount")),
        "keywords": branding.get("keywords"),
        "topics": [t.rsplit("/", 1)[-1].replace("_", " ") for t in topics],
        "uploads_playlist_id": item.get("contentDetails", {}).get("relatedPlaylists", {}).get("uploads"),
        "thumbnail_url": snippet.get("thumbnails", {}).get("high", {}).get("url"),
        "url": f"https://www.youtube.com/channel/{item['id']}",
    }


class YouTubeApi:
    """Thin async client over both YouTube APIs with caching and quota control."""

    def __init__(self, api_key: str, store: storage_lib.Storage, daily_quota_units: int, cache_ttl_seconds: int):
        self._api_key = api_key
        self._store = store
        self._daily_quota = daily_quota_units
        self._ttl = cache_ttl_seconds
        self._http = httpx.AsyncClient(
            http2=True,
            timeout=httpx.Timeout(20.0, connect=10.0),
            limits=httpx.Limits(max_connections=20, max_keepalive_connections=10),
            headers={"Accept-Encoding": "gzip", "User-Agent": "youtube-insights-mcp/1.0 (gzip)"},
        )
        self._shorts_semaphore = asyncio.Semaphore(8)

    async def aclose(self) -> None:
        await self._http.aclose()

    # ---- low level -----------------------------------------------------
    def _charge(self, method: str, calls: int = 1) -> None:
        units = QUOTA_COST.get(method, 1) * calls
        if not units:
            return
        if self._store.quota_used_today() + units > self._daily_quota:
            raise QuotaExceededError(
                f"Daily quota budget reached ({self._daily_quota} units). It resets at midnight Pacific time. "
                "Cached data is still available."
            )
        self._store.add_quota(units)

    async def _request(
        self,
        verb: str,
        url: str,
        *,
        params: dict | None = None,
        json_body: dict | None = None,
        bearer: str | None = None,
    ) -> dict[str, Any]:
        headers = {"Authorization": f"Bearer {bearer}"} if bearer else {}
        query = dict(params or {})
        if not bearer:
            query["key"] = self._api_key
        for attempt in range(_MAX_RETRIES + 1):
            try:
                resp = await self._http.request(verb, url, params=query, json=json_body, headers=headers)
            except httpx.HTTPError as exc:
                if attempt == _MAX_RETRIES:
                    raise YouTubeApiError(f"Network error talking to YouTube: {type(exc).__name__}") from None
                await asyncio.sleep(0.5 * 2**attempt)
                continue
            if resp.status_code in _RETRY_STATUS and attempt < _MAX_RETRIES:
                await asyncio.sleep(0.5 * 2**attempt)
                continue
            if resp.status_code >= 400:
                raise YouTubeApiError(self._error_message(resp))
            return resp.json() if resp.content else {}
        raise YouTubeApiError("YouTube API kept failing after retries.")

    @staticmethod
    def _error_message(resp: httpx.Response) -> str:
        try:
            err = resp.json().get("error", {})
            reason = (err.get("errors") or [{}])[0].get("reason", "")
            message = err.get("message", "")
        except (ValueError, AttributeError):
            reason, message = "", resp.text[:200]
        hint = {
            "quotaExceeded": " Google's daily quota for this API key is used up.",
            "keyInvalid": " Check YTMCP_YOUTUBE_API_KEY.",
            "forbidden": " The signed-in Google account may not own this channel.",
            "insufficientPermissions": " Sign in again and grant the YouTube Analytics permission.",
        }.get(reason, "")
        return f"YouTube API error {resp.status_code} {reason}: {message}{hint}".strip()

    async def data_get(
        self,
        method: str,
        params: dict[str, Any],
        *,
        bearer: str | None = None,
        cache: bool = True,
        refresh: bool = False,
    ) -> dict[str, Any]:
        """Calls a Data API list method, e.g. method='videos.list'. refresh=True skips the cache read."""
        resource = method.split(".")[0]
        cache_key = (
            "data:" + hashlib.sha256(json.dumps([method, params, bool(bearer)], sort_keys=True).encode()).hexdigest()
        )
        if cache and not bearer and not refresh:
            hit = self._store.cache_get(cache_key)
            if hit is not None:
                return hit
        self._charge(method)
        result = await self._request("GET", f"{DATA_API_URL}/{resource}", params=params, bearer=bearer)
        if cache and not bearer:
            self._store.cache_put(cache_key, result, self._ttl)
        return result

    async def analytics_report(self, params: dict[str, Any], bearer: str) -> dict[str, Any]:
        """Runs a YouTube Analytics API v2 reports.query."""
        cache_key = (
            "yta:"
            + hashlib.sha256(
                json.dumps([params, hashlib.sha256(bearer.encode()).hexdigest()[:16]], sort_keys=True).encode()
            ).hexdigest()
        )
        hit = self._store.cache_get(cache_key)
        if hit is not None:
            return hit
        result = await self._request("GET", ANALYTICS_API_URL, params=params, bearer=bearer)
        self._store.cache_put(cache_key, result, min(self._ttl, 1800))
        return result

    async def update_video(self, body: dict[str, Any], bearer: str) -> dict[str, Any]:
        self._charge("videos.update")
        return await self._request(
            "PUT", f"{DATA_API_URL}/videos", params={"part": "snippet"}, json_body=body, bearer=bearer
        )

    # ---- channels ------------------------------------------------------
    async def resolve_channel_id(self, query: str, allow_search: bool = True) -> str:
        """Turns a channel ID, @handle, channel URL, or name into a channel ID."""
        q = query.strip()
        url_match = re.search(r"youtube\.com/(channel/(UC[\w-]{22})|@([\w.\-]+)|c/([\w.\-]+)|user/([\w.\-]+))", q)
        if _CHANNEL_ID_RE.fullmatch(q):
            return q
        if url_match:
            channel_id, handle, custom, user = (
                url_match.group(2),
                url_match.group(3),
                url_match.group(4),
                url_match.group(5),
            )
            if channel_id:
                return channel_id
            if handle:
                q = "@" + handle
            elif user:
                found = await self.data_get("channels.list", {"part": "id", "forUsername": user})
                if found.get("items"):
                    return found["items"][0]["id"]
                q = custom or user
            else:
                q = custom
        if _HANDLE_RE.fullmatch(q):
            found = await self.data_get(
                "channels.list", {"part": "id", "forHandle": q if q.startswith("@") else "@" + q}
            )
            if found.get("items"):
                return found["items"][0]["id"]
        if not allow_search:
            raise YouTubeApiError(f"Channel not found: {query!r}")
        found = await self.data_get("search.list", {"part": "snippet", "q": q, "type": "channel", "maxResults": 1})
        if not found.get("items"):
            raise YouTubeApiError(f"Channel not found: {query!r}")
        return found["items"][0]["snippet"]["channelId"]

    async def get_channels(self, channel_ids: list[str], bearer: str | None = None) -> list[dict[str, Any]]:
        params = {"part": "snippet,statistics,contentDetails,brandingSettings,topicDetails"}
        if bearer and not channel_ids:
            params["mine"] = "true"
        else:
            params["id"] = ",".join(channel_ids[:50])
        data = await self.data_get("channels.list", params, bearer=bearer)
        return [normalize_channel(item) for item in data.get("items", [])]

    # ---- videos --------------------------------------------------------
    async def list_upload_ids(self, uploads_playlist_id: str, max_videos: int) -> list[str]:
        """Pages through a channel's uploads playlist (1 unit per 50 videos)."""
        ids: list[str] = []
        token = None
        while len(ids) < max_videos:
            params = {"part": "contentDetails", "playlistId": uploads_playlist_id, "maxResults": 50}
            if token:
                params["pageToken"] = token
            page = await self.data_get("playlistItems.list", params)
            ids.extend(item["contentDetails"]["videoId"] for item in page.get("items", []))
            token = page.get("nextPageToken")
            if not token:
                break
        return ids[:max_videos]

    async def get_videos(
        self, video_ids: list[str], detect_shorts: bool = True, refresh: bool = False
    ) -> list[dict[str, Any]]:
        """Fetches full records for any number of videos, 50 per request, in parallel."""
        chunks = [video_ids[i : i + 50] for i in range(0, len(video_ids), 50)]
        pages = await asyncio.gather(
            *(
                self.data_get(
                    "videos.list",
                    {"part": "snippet,statistics,contentDetails,status", "id": ",".join(c)},
                    refresh=refresh,
                )
                for c in chunks
            )
        )
        order = {vid: i for i, vid in enumerate(video_ids)}
        videos = sorted(
            (normalize_video(it) for p in pages for it in p.get("items", [])), key=lambda v: order.get(v["video_id"], 0)
        )
        if detect_shorts:
            flags = await self.shorts_flags([v for v in videos])
            for v in videos:
                v["is_short"] = flags.get(v["video_id"], False)
        if videos:
            self._store.save_videos(videos)
        return videos

    async def shorts_flags(self, videos: list[dict[str, Any]]) -> dict[str, bool]:
        """Detects Shorts: short duration + the /shorts/ URL does not redirect. Cached forever."""
        known = self._store.shorts_flags([v["video_id"] for v in videos])
        todo = [v for v in videos if v["video_id"] not in known]
        result = dict(known)
        long_ones = {v["video_id"]: False for v in todo if v["duration_seconds"] > _SHORTS_MAX_SECONDS}
        result.update(long_ones)

        async def check(video_id: str) -> tuple[str, bool]:
            async with self._shorts_semaphore:
                try:
                    resp = await self._http.head(SHORTS_URL.format(video_id=video_id), follow_redirects=False)
                    return video_id, resp.status_code == 200
                except httpx.HTTPError:
                    return video_id, True  # Network hiccup: duration already says it is short.

        checked = dict(await asyncio.gather(*(check(v["video_id"]) for v in todo if v["video_id"] not in long_ones)))
        result.update(checked)
        self._store.save_shorts_flags({**long_ones, **checked})
        return result

    async def get_comments(self, video_id: str, max_results: int, order: str) -> list[dict[str, Any]]:
        data = await self.data_get(
            "commentThreads.list",
            {
                "part": "snippet",
                "videoId": video_id,
                "maxResults": min(max_results, 100),
                "order": order,
                "textFormat": "plainText",
            },
        )
        out = []
        for item in data.get("items", []):
            top = item["snippet"]["topLevelComment"]["snippet"]
            out.append(
                {
                    "author": top.get("authorDisplayName"),
                    "text": top.get("textDisplay"),
                    "likes": top.get("likeCount"),
                    "published_at": top.get("publishedAt"),
                    "replies": item["snippet"].get("totalReplyCount", 0),
                }
            )
        return out

    async def search(self, params: dict[str, Any]) -> dict[str, Any]:
        return await self.data_get("search.list", params)
