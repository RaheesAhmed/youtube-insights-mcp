from youtube_insights_mcp import analysis


def _video(i, views, short=True, title="Physics trick"):
    return {
        "video_id": f"v{i}",
        "title": title,
        "views": views,
        "likes": views // 50,
        "comments": 1,
        "engagement_rate_pct": 2.0,
        "duration_seconds": 40 if short else 400,
        "is_short": short,
        "tags": ["physics"],
        "published_at": f"2026-11-{10 + i:02d}T{5 + i:02d}:00:00Z",
    }


def test_channel_insights_splits_and_outliers():
    videos = [_video(i, 1000) for i in range(5)] + [_video(6, 9000, title="Impossible physics trick")]
    videos.append(_video(7, 10, short=False))
    report = analysis.channel_insights(videos)
    assert report["shorts"]["count"] == 6 and report["long_form"]["count"] == 1
    assert report["outliers_3x_median"][0]["video_id"] == "v6"
    assert report["cadence"]["uploads_per_week"] > 0
    assert report["top_tags"][0] == {"tag": "physics", "videos": 7}


def test_view_velocity():
    rows = analysis.view_velocity([_video(1, 100)], {"v1": [(0, 100), (3600, 400)]})
    assert rows[0]["views_per_hour"] == 300


def test_rows_to_dicts():
    report = {"columnHeaders": [{"name": "day"}, {"name": "views"}], "rows": [["2026-11-01", 5]]}
    assert analysis.rows_to_dicts(report) == [{"day": "2026-11-01", "views": 5}]
