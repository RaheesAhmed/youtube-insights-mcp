"""Pure analysis helpers over normalized video records. No I/O, easy to test."""

from __future__ import annotations

import collections
import datetime
import re
import statistics
from typing import Any

_WORD_RE = re.compile(r"[a-zA-Z][a-zA-Z']{2,}")
_STOP_WORDS = frozenset(
    "the and for with you your this that are was what how why when from into they them then than"
    " its it's our out not all can will just one has have had who about".split()
)
_WEEKDAYS = ("Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun")


def _published(video: dict[str, Any]) -> datetime.datetime | None:
    raw = video.get("published_at")
    return datetime.datetime.fromisoformat(raw.replace("Z", "+00:00")) if raw else None


def _median(values: list[float]) -> float | None:
    return round(statistics.median(values), 1) if values else None


def _summary(videos: list[dict[str, Any]]) -> dict[str, Any]:
    views = [v["views"] or 0 for v in videos]
    eng = [v["engagement_rate_pct"] for v in videos if v.get("engagement_rate_pct") is not None]
    return {
        "count": len(videos),
        "total_views": sum(views),
        "median_views": _median(views),
        "mean_views": round(statistics.fmean(views), 1) if views else None,
        "max_views": max(views) if views else None,
        "median_engagement_rate_pct": _median(eng),
        "median_duration_seconds": _median([v["duration_seconds"] for v in videos]),
    }


def _compact(video: dict[str, Any]) -> dict[str, Any]:
    keys = (
        "video_id",
        "title",
        "views",
        "likes",
        "comments",
        "engagement_rate_pct",
        "duration_seconds",
        "published_at",
        "is_short",
        "url",
    )
    return {k: video.get(k) for k in keys}


def channel_insights(videos: list[dict[str, Any]], top_n: int = 5) -> dict[str, Any]:
    """Builds a performance report from a channel's videos.

    Args:
      videos: Normalized video records (newest first is fine).
      top_n: How many top / bottom videos to list.

    Returns:
      A dict with overall stats, Shorts vs long-form split, cadence, timing,
      title and tag patterns, and outliers.
    """
    if not videos:
        return {"error": "No videos to analyze."}
    shorts = [v for v in videos if v.get("is_short")]
    longs = [v for v in videos if not v.get("is_short")]
    dated = sorted((d, v) for v in videos if (d := _published(v)))
    by_views = sorted(videos, key=lambda v: v["views"] or 0, reverse=True)
    median_views = statistics.median([v["views"] or 0 for v in videos]) or 1

    cadence = None
    if len(dated) >= 2:
        span_days = max((dated[-1][0] - dated[0][0]).total_seconds() / 86400, 1)
        gaps = [(b[0] - a[0]).total_seconds() / 3600 for a, b in zip(dated, dated[1:], strict=False)]
        cadence = {
            "first_upload": dated[0][0].isoformat(),
            "latest_upload": dated[-1][0].isoformat(),
            "uploads_per_week": round(7 * len(dated) / span_days, 2),
            "median_hours_between_uploads": _median(gaps),
        }

    def bucket_stats(key_fn) -> list[dict[str, Any]]:
        buckets: dict[Any, list[int]] = collections.defaultdict(list)
        for d, v in dated:
            buckets[key_fn(d)].append(v["views"] or 0)
        rows = [{"bucket": k, "videos": len(x), "median_views": _median(x)} for k, x in buckets.items()]
        return sorted(rows, key=lambda r: r["median_views"] or 0, reverse=True)

    tag_counter = collections.Counter(t.lower() for v in videos for t in v.get("tags", []))
    word_views: dict[str, list[int]] = collections.defaultdict(list)
    for v in videos:
        for word in {w.lower() for w in _WORD_RE.findall(v.get("title") or "")} - _STOP_WORDS:
            word_views[word].append(v["views"] or 0)
    title_words = sorted(
        ({"word": w, "videos": len(x), "median_views": _median(x)} for w, x in word_views.items() if len(x) >= 2),
        key=lambda r: r["median_views"] or 0,
        reverse=True,
    )[:15]
    lengths = [len(v.get("title") or "") for v in videos]

    return {
        "overall": _summary(videos),
        "shorts": _summary(shorts) if shorts else None,
        "long_form": _summary(longs) if longs else None,
        "cadence": cadence,
        "best_publish_hour_utc": bucket_stats(lambda d: d.hour)[:6],
        "best_publish_weekday_utc": bucket_stats(lambda d: _WEEKDAYS[d.weekday()]),
        "title_patterns": {
            "median_title_length": _median(lengths),
            "titles_with_question_mark_pct": round(
                100 * sum("?" in (v.get("title") or "") for v in videos) / len(videos), 1
            ),
            "titles_with_numbers_pct": round(
                100 * sum(bool(re.search(r"\d", v.get("title") or "")) for v in videos) / len(videos), 1
            ),
            "best_title_words": title_words,
        },
        "top_tags": [{"tag": t, "videos": n} for t, n in tag_counter.most_common(20)],
        "videos_without_tags": sum(1 for v in videos if not v.get("tags")),
        "top_videos": [_compact(v) for v in by_views[:top_n]],
        "bottom_videos": [_compact(v) for v in by_views[-top_n:][::-1]],
        "outliers_3x_median": [_compact(v) for v in by_views if (v["views"] or 0) >= 3 * median_views][:10],
    }


def view_velocity(videos: list[dict[str, Any]], snapshots: dict[str, list[tuple[float, int]]]) -> list[dict[str, Any]]:
    """Views gained per hour between the two latest stored snapshots of each video."""
    rows = []
    for v in videos:
        snaps = snapshots.get(v["video_id"], [])
        if len(snaps) < 2:
            continue
        (t0, v0), (t1, v1) = snaps[-2], snaps[-1]
        hours = max((t1 - t0) / 3600, 1e-6)
        rows.append(
            {
                **_compact(v),
                "views_gained": v1 - v0,
                "hours_between_snapshots": round(hours, 2),
                "views_per_hour": round((v1 - v0) / hours, 1),
            }
        )
    return sorted(rows, key=lambda r: r["views_per_hour"], reverse=True)


def rows_to_dicts(report: dict[str, Any]) -> list[dict[str, Any]]:
    """Turns an Analytics API response (columnHeaders + rows) into a list of dicts."""
    names = [h["name"] for h in report.get("columnHeaders", [])]
    return [dict(zip(names, row, strict=False)) for row in report.get("rows", []) or []]
