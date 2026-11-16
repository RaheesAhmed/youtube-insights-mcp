import httpx
import pytest
import respx

from youtube_insights_mcp import storage, youtube_api


@pytest.mark.parametrize(
    ("raw", "seconds"), [("PT45S", 45), ("PT1M5S", 65), ("PT2H", 7200), ("P1DT1S", 86401), ("", 0)]
)
def test_parse_duration(raw, seconds):
    assert youtube_api.parse_duration(raw) == seconds


@pytest.mark.parametrize(
    "raw",
    [
        "dQw4w9WgXcQ",
        "https://youtu.be/dQw4w9WgXcQ",
        "https://www.youtube.com/shorts/dQw4w9WgXcQ",
        "https://www.youtube.com/watch?v=dQw4w9WgXcQ&t=3",
    ],
)
def test_extract_video_id(raw):
    assert youtube_api.extract_video_id(raw) == "dQw4w9WgXcQ"


def test_extract_video_id_rejects_garbage():
    with pytest.raises(youtube_api.YouTubeApiError):
        youtube_api.extract_video_id("not a video")


def _api(tmp_path, quota=100):
    return youtube_api.YouTubeApi("k", storage.Storage(tmp_path / "db.sqlite3"), quota, 60)


@respx.mock
async def test_resolve_handle_is_cached_and_costs_one_unit(tmp_path):
    route = respx.get(f"{youtube_api.DATA_API_URL}/channels").mock(
        return_value=httpx.Response(200, json={"items": [{"id": "UC" + "a" * 22}]})
    )
    api = _api(tmp_path)
    assert await api.resolve_channel_id("@someone") == "UC" + "a" * 22
    assert await api.resolve_channel_id("https://www.youtube.com/@someone") == "UC" + "a" * 22
    assert route.call_count == 1
    assert api._store.quota_used_today() == 1
    await api.aclose()


@respx.mock
async def test_quota_budget_is_enforced(tmp_path):
    respx.get(f"{youtube_api.DATA_API_URL}/search").mock(return_value=httpx.Response(200, json={"items": []}))
    api = _api(tmp_path, quota=50)
    with pytest.raises(youtube_api.QuotaExceededError):
        await api.search({"q": "x"})
    await api.aclose()


@respx.mock
async def test_error_message_hides_api_key(tmp_path):
    respx.get(f"{youtube_api.DATA_API_URL}/videos").mock(
        return_value=httpx.Response(400, json={"error": {"message": "Bad", "errors": [{"reason": "keyInvalid"}]}})
    )
    api = _api(tmp_path)
    with pytest.raises(youtube_api.YouTubeApiError) as err:
        await api.data_get("videos.list", {"id": "x"})
    assert "keyInvalid" in str(err.value) and "k=" not in str(err.value)
    await api.aclose()


@respx.mock
async def test_shorts_detection(tmp_path):
    respx.head(youtube_api.SHORTS_URL.format(video_id="shortvid001")).mock(return_value=httpx.Response(200))
    respx.head(youtube_api.SHORTS_URL.format(video_id="normalvid01")).mock(return_value=httpx.Response(303))
    api = _api(tmp_path)
    flags = await api.shorts_flags(
        [
            {"video_id": "shortvid001", "duration_seconds": 40},
            {"video_id": "normalvid01", "duration_seconds": 50},
            {"video_id": "longvideo01", "duration_seconds": 900},
        ]
    )
    assert flags == {"shortvid001": True, "normalvid01": False, "longvideo01": False}
    await api.aclose()
