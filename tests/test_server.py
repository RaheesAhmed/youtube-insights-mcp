from fastmcp import Client

from youtube_insights_mcp import auth, server


async def test_tools_registered_and_read_only(settings):
    mcp = server.create_server(settings)
    async with Client(mcp) as client:
        tools = {t.name: t for t in await client.list_tools()}
    assert "analyze_channel" in tools and "update_video_metadata" not in tools
    assert tools["get_channel_overview"].annotations.readOnlyHint is True


async def test_bearer_verifier():
    verifier = auth.BearerTokenVerifier(("a" * 40, "b" * 40))
    assert await verifier.verify_token("b" * 40) is not None
    assert await verifier.verify_token("c" * 40) is None


async def test_owner_video_performance_joins_titles(settings, monkeypatch):
    import httpx
    import respx

    from youtube_insights_mcp import youtube_api

    async def fake_token(self):
        return "owner-token"

    monkeypatch.setattr(auth.OwnerTokenSource, "get", fake_token)
    mcp = server.create_server(settings)
    with respx.mock:
        analytics = respx.get(youtube_api.ANALYTICS_API_URL).mock(
            return_value=httpx.Response(
                200,
                json={
                    "columnHeaders": [{"name": "video"}, {"name": "views"}, {"name": "averageViewPercentage"}],
                    "rows": [["dQw4w9WgXcQ", 1200, 71.5]],
                },
            )
        )
        respx.get(f"{youtube_api.DATA_API_URL}/videos").mock(
            return_value=httpx.Response(
                200,
                json={
                    "items": [
                        {
                            "id": "dQw4w9WgXcQ",
                            "snippet": {"title": "My Short", "channelId": "UC" + "a" * 22},
                            "contentDetails": {"duration": "PT40S"},
                            "statistics": {"viewCount": "1200"},
                        }
                    ]
                },
            )
        )
        async with Client(mcp) as client:
            res = await client.call_tool("get_my_video_performance", {"content_type": "shorts"})
    sent = analytics.calls[0].request
    assert sent.headers["Authorization"] == "Bearer owner-token"
    assert "creatorContentType%3D%3DSHORTS" in str(sent.url) and "key=" not in str(sent.url)
    row = res.structured_content["videos"][0]
    assert row["title"] == "My Short" and row["averageViewPercentage"] == 71.5
