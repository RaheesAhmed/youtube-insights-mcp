<div align="center">

# YouTube Insights MCP

**Ask Claude anything about any YouTube channel, including the private analytics of your own.**

[![Python](https://img.shields.io/badge/python-3.12%2B-3776AB?logo=python&logoColor=white)](https://www.python.org/)
[![MCP](https://img.shields.io/badge/MCP-2026--07--28-8A2BE2)](https://modelcontextprotocol.io/)
[![FastMCP](https://img.shields.io/badge/built%20with-FastMCP-0ea5e9)](https://gofastmcp.com/)
[![uv](https://img.shields.io/badge/uv-managed-DE5FE9?logo=astral&logoColor=white)](https://docs.astral.sh/uv/)
[![License: MIT](https://img.shields.io/badge/license-MIT-green.svg)](LICENSE)

![YouTube Insights MCP in Claude](banner.png)

</div>

YouTube Insights MCP is an open-source [Model Context Protocol](https://modelcontextprotocol.io/) server. It connects Claude (or any MCP client) to the **YouTube Data API v3** and the **YouTube Analytics API**. Instead of clicking through YouTube Studio, you just ask:

> *"Audit my channel and tell me why my Shorts stop at 1,200 views."*
> *"Compare my channel with @mkbhd and @veritasium."*
> *"Show the retention curve of my last Short. Where do people leave?"*
> *"Which of my videos brought the most subscribers this month?"*

---

## Contents
- [Features](#features)
- [Quick start (5 minutes)](#quick-start)
- [Connect to Claude](#connect-to-claude)
- [Tools](#tools)
- [Configuration](#configuration)
- [Deploy](#deploy)
- [Troubleshooting](#troubleshooting)
- [Security](#security)
- [Development](#development)

---

## Features

| | |
|---|---|
| 🔎 **Any public channel** | Profile, every upload with tags and stats, comments, Shorts vs long-form detection, full performance report, side-by-side comparisons, view velocity, niche search |
| 📊 **Your own channel** | Engaged views, average % viewed, watch time, subscribers per video, retention curves, audience by country / age / gender / traffic source / device |
| ✏️ **Safe editing** (opt-in) | Update titles, descriptions and tags. Every change is previewed and needs your approval |
| ⚡ **Fast** | HTTP/2 connection pooling, parallel batched requests and a local SQLite cache. Repeat questions answer in milliseconds and cost **0 quota** |
| 🔐 **Secure by default** | Bearer-token or Google OAuth 2.1 (PKCE) auth, email allowlist, read-only scopes, daily quota guard, no secrets in logs |
| 🌐 **Modern MCP** | Streamable HTTP, stateless (spec `2026-07-28`), so it runs locally or behind any load balancer |

---

## Quick start

### What you need
- [uv](https://docs.astral.sh/uv/getting-started/installation/) (installs Python for you)
- A Google account and a free [Google Cloud](https://console.cloud.google.com) project

### Step 1: Get Google credentials

1. In [Google Cloud Console](https://console.cloud.google.com), create a project.
2. **APIs & Services → Library**: enable **YouTube Data API v3** and **YouTube Analytics API**.
3. **Credentials → Create credentials → API key**. Copy the key.
4. *(Only for your own analytics)* **Credentials → Create credentials → OAuth client ID → Web application**
   - Authorized redirect URI: `http://localhost:8765/oauth2callback`
   - Download the JSON and save it as `secrets/google_client_secret.json`
5. *(Only for your own analytics)* **OAuth consent screen / Audience**
   - User type: **External**, status **Testing**
   - **Test users → Add users**: the Google account that owns your channel

### Step 2: Install and configure

```bash
git clone https://github.com/RaheesAhmed/youtube-insights-mcp.git
cd youtube-insights-mcp
uv sync
```

Copy the example config:

```bash
cp .env.example .env          # macOS / Linux
copy .env.example .env        # Windows
```

Open `.env` and set at least:

```env
YTMCP_YOUTUBE_API_KEY=your-api-key
YTMCP_BEARER_TOKENS=paste-output-of-gen-token
```

```bash
uv run youtube-insights-mcp gen-token     # creates a random access token
uv run youtube-insights-mcp doctor        # checks your setup
```

### Step 3: (Optional) Sign in for your own analytics

```bash
uv run youtube-insights-mcp login --email you@gmail.com
```

Pick the account that owns the channel. If the channel is a brand account, pick the channel on the next screen. On a server with no browser, add `--manual` and paste back the address the browser lands on.

### Step 4: Run

```bash
uv run youtube-insights-mcp serve
# ➜ http://127.0.0.1:8080/mcp
```

---

## Connect to Claude

### Claude Code

```bash
claude mcp add --transport http youtube-insights http://127.0.0.1:8080/mcp \
  --header "Authorization: Bearer YOUR_TOKEN"
```

### Claude.ai / Claude Desktop (custom connector)

Custom connectors need a public HTTPS address and Google sign-in.

1. Expose the server, for example with `cloudflared tunnel --url http://localhost:8080`, or [deploy](#deploy) it.
2. In `.env`:
   ```env
   YTMCP_AUTH_MODE=google
   YTMCP_PUBLIC_BASE_URL=https://your-public-host
   YTMCP_JWT_SIGNING_KEY=output-of-gen-token
   YTMCP_ALLOWED_EMAILS=you@gmail.com
   ```
3. Add `https://your-public-host/auth/callback` to the OAuth client's redirect URIs.
4. In Claude: **Settings → Connectors → Add custom connector**, enter `https://your-public-host/mcp`, then **Connect**.

Each person signs in with their own Google account, so "my analytics" always means *their* channel.

### Other MCP clients
Any client that supports Streamable HTTP works. Point it at `http://127.0.0.1:8080/mcp` and send `Authorization: Bearer <token>`.

---

## Tools

### Any channel (API key only)

| Tool | What it does | Quota |
|---|---|---|
| `get_channel_overview` | Subscribers, views, video count, description, keywords, topics | 1–2 |
| `list_channel_videos` | Every upload with title, tags, stats, duration and Short flag | ~1 / 25 videos |
| `get_video_details` | Full metadata for up to 50 videos | 1 |
| `get_video_comments` | Top comments with likes and replies | 1 |
| `analyze_channel` | Shorts vs long, upload cadence, best hour/weekday, winning title words, top tags, outliers | ~1 / 25 videos |
| `compare_channels` | 2–6 channels side by side | per channel |
| `get_view_velocity` | Views per hour since the last check | ~1 / 25 videos |
| `search_youtube` | Find videos or channels in a niche | **100** |
| `get_quota_status` | Quota used today and remaining | 0 |

### Your channel (needs `login` or Google mode)

| Tool | What it does |
|---|---|
| `get_my_channel` | Your channel profile and totals |
| `get_my_video_performance` | Per video: views, engaged views, avg % viewed, watch time, likes, shares, subscribers |
| `get_my_audience` | By country, age & gender, traffic source, device, subscribed status, content type or day |
| `get_my_retention_curve` | 100-point retention curve, the steepest drop, and comparison with similar videos |
| `query_my_analytics` | Any YouTube Analytics metrics, dimensions and filters |
| `record_studio_metrics` / `get_studio_metrics` | Save numbers only Studio shows (Shorts viewed vs swiped, impressions, CTR) |
| `update_video_metadata` | Edit title, description or tags with preview + confirm *(only when `YTMCP_ENABLE_WRITE_TOOLS=true`)* |

> **Good to know:** no Google API gives the Shorts *viewed vs swiped away* percentage directly. `engagedViews ÷ views` from `get_my_video_performance` is a close stand-in.

---

## Configuration

All settings live in `.env`. See [`.env.example`](.env.example) for the full list.

| Variable | Default | Description |
|---|---|---|
| `YTMCP_YOUTUBE_API_KEY` | **required** | Google Cloud API key |
| `YTMCP_GOOGLE_CLIENT_SECRETS_FILE` | `./secrets/google_client_secret.json` | OAuth client JSON (for your analytics) |
| `YTMCP_AUTH_MODE` | `token` | `token`, `google` or `none` (localhost only) |
| `YTMCP_BEARER_TOKENS` | | Comma-separated tokens, 32+ characters each |
| `YTMCP_PUBLIC_BASE_URL` | | Public `https://` URL (google mode) |
| `YTMCP_JWT_SIGNING_KEY` | | Random 32+ characters (google mode) |
| `YTMCP_ALLOWED_EMAILS` | anyone | Google accounts allowed in (google mode) |
| `YTMCP_HOST` / `YTMCP_PORT` | `127.0.0.1` / `8080` | Listen address |
| `YTMCP_DATA_DIR` | `./data` | SQLite cache and sign-in token |
| `YTMCP_DAILY_QUOTA_UNITS` | `10000` | Max Data API units per day |
| `YTMCP_CACHE_TTL_SECONDS` | `3600` | How long API responses are reused |
| `YTMCP_ENABLE_WRITE_TOOLS` | `false` | Allow editing your videos (run `login` again after enabling) |

### Commands

| Command | Purpose |
|---|---|
| `uv run youtube-insights-mcp serve` | Start the server |
| `uv run youtube-insights-mcp login [--email] [--manual]` | Sign in as channel owner |
| `uv run youtube-insights-mcp doctor` | Check config and API access |
| `uv run youtube-insights-mcp gen-token` | Generate a secure token |

---

## Deploy

### Docker

```bash
docker build -t youtube-insights-mcp .
docker run -d -p 8080:8080 --env-file .env \
  -v ./data:/app/data -v ./secrets:/app/secrets:ro \
  youtube-insights-mcp
```

The image runs as a non-root user and has a health check at `GET /healthz`. The same image works on **Google Cloud Run, Fly.io, Railway and Render**. Mount `/app/data` on a volume so the cache and sign-in survive restarts.

---

## Troubleshooting

| Problem | Fix |
|---|---|
| `Error 403: org_internal` | Your consent screen is *Internal*. Switch it to **External → Testing** and add yourself as a test user |
| `redirect_uri_mismatch` | Add exactly `http://localhost:8765/oauth2callback` to the OAuth client |
| `Access blocked` / app not verified | Add your account under **Test users**, then click **Advanced → Continue** |
| Login uses the wrong Google account | Use `login --email you@gmail.com` and pick the right account in the chooser |
| `No Google sign-in stored` | Run `login`, and run `serve` from the same folder (or set an absolute `YTMCP_DATA_DIR`) |
| `update_video_metadata` is missing | Set `YTMCP_ENABLE_WRITE_TOOLS=true`, run `login` again, restart `serve` and reconnect your client |
| `quotaExceeded` | Google's 10,000 units/day are used up. Cached answers still work; quota resets at midnight Pacific |
| Port 8080 already in use | Set `YTMCP_PORT=8081`, or stop the old process |

---

## Security

- **Auth modes:** `none` only starts on `127.0.0.1`. `token` requires 32+ character tokens, compared in constant time. `google` uses OAuth 2.1 with PKCE and dynamic client registration.
- **Google tokens** stay on the server. Clients only receive a token signed by this server.
- **Read-only by default.** The edit permission is requested only when you enable write tools, and every edit needs a preview and your confirmation.
- **Private files:** the sign-in file is stored with owner-only permissions, and `.env`, `secrets/` and `data/` are git-ignored.
- **Quota guard:** a local daily budget stops runaway agents from using up your quota.

Found a vulnerability? Please open a private security advisory on GitHub instead of a public issue.

---

## Development

```bash
uv sync                       # install with dev tools
uv run pytest                 # offline tests (HTTP mocked)
uv run ruff check .           # lint
uv run python scripts/smoke_test.py @somechannel   # live test against a running server
```

```
src/youtube_insights_mcp/
├── cli.py            # serve | login | doctor | gen-token
├── config.py         # settings from .env + validation
├── auth.py           # token / Google OAuth / allowlist
├── google_oauth.py   # owner login (PKCE) + auto refresh
├── youtube_api.py    # async Data + Analytics client, retries, quota, cache
├── storage.py        # SQLite: cache, snapshots, Studio metrics, quota
├── analysis.py       # channel report and velocity
├── server.py         # FastMCP app, stateless Streamable HTTP
└── tools/
    ├── public_tools.py
    └── owner_tools.py
```

### Contributing
Pull requests are welcome. Please run `uv run pytest` and `uv run ruff check .` before opening one. For larger changes, open an issue first so we can agree on the approach.

---

## License

[MIT](LICENSE) © Rahees Ahmed

<div align="center">

If this project helps you, please give it a ⭐ on GitHub. It helps others find it.

</div>