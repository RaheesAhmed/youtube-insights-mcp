"""Live end-to-end test over Streamable HTTP. Usage: uv run python scripts/smoke_test.py [channel]"""

import asyncio
import json
import os
import sys
import time

from dotenv import load_dotenv
from fastmcp import Client
from fastmcp.client.transports import StreamableHttpTransport

load_dotenv()
URL = os.environ.get("YTMCP_TEST_URL", f"http://127.0.0.1:{os.environ.get('YTMCP_PORT', '8080')}/mcp")
TOKEN = os.environ.get("YTMCP_BEARER_TOKENS", "").split(",")[0]
CHANNEL = sys.argv[1] if len(sys.argv) > 1 else "@illegalphysics"


def short(obj, n=700):
    text = json.dumps(obj, default=str)
    return text[:n] + ("..." if len(text) > n else "")


async def main() -> None:
    transport = StreamableHttpTransport(URL, headers={"Authorization": f"Bearer {TOKEN}"} if TOKEN else None)
    async with Client(transport) as client:
        tools = await client.list_tools()
        print(f"{len(tools)} tools:", ", ".join(t.name for t in tools))
        calls = [
            ("get_channel_overview", {"channel": CHANNEL}),
            ("list_channel_videos", {"channel": CHANNEL, "max_videos": 10, "sort_by": "views"}),
            ("analyze_channel", {"channel": CHANNEL, "max_videos": 60}),
            ("compare_channels", {"channels": [CHANNEL, "@MathMotion97"], "max_videos_each": 20}),
            ("get_video_details", {"videos": ["https://youtu.be/dQw4w9WgXcQ"]}),
            ("get_video_comments", {"video": "dQw4w9WgXcQ", "max_results": 3}),
            ("get_view_velocity", {"channel": CHANNEL, "max_videos": 5}),
            ("record_studio_metrics", {"video": "dQw4w9WgXcQ", "viewed_pct": 62.5}),
            ("get_studio_metrics", {}),
            ("get_quota_status", {}),
            ("get_my_channel", {}),
        ]
        for name, params in calls:
            t0 = time.perf_counter()
            try:
                res = await client.call_tool(name, params, raise_on_error=False)
                status = "ERROR" if res.is_error else "ok"
                body = res.structured_content if res.structured_content is not None else res.content[0].text
            except Exception as exc:  # noqa: BLE001
                status, body = "EXC", str(exc)
            print(f"\n[{status}] {name} ({(time.perf_counter() - t0) * 1000:.0f} ms): {short(body)}")


asyncio.run(main())
