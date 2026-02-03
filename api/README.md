# yt-dlp Metadata API

HTTP API for video metadata only (no download). Uses yt-dlp extraction under the hood.

## Install

From the repo root:

```bash
pip install -e ".[api]"
```

## Run

```bash
yt-dlp-api
```

Or:

```bash
python -m api
```

Environment variables:

- `YT_DLP_API_HOST` (default: `127.0.0.1`) – bind address
- `YT_DLP_API_PORT` (default: `8000`) – port (or `PORT` on Render)
- **`API_SECRET`** (required) – secret used to authenticate requests; must be sent as a Bearer token (see below)

Example: `YT_DLP_API_PORT=9000 yt-dlp-api`

### Authentication

Every request must include the shared secret in the **Authorization** header as a Bearer token:

```http
Authorization: Bearer YOUR_API_SECRET
```

Set `API_SECRET` in your environment (e.g. in a `.env` file locally). **On Render:** open your service → **Environment** → add a variable `API_SECRET` with your chosen secret (Render will inject it at runtime). If `API_SECRET` is not set, the API returns 503.

Example with curl:

```bash
curl -H "Authorization: Bearer YOUR_API_SECRET" "http://127.0.0.1:8000/youtube/video?url=..."
```

## Endpoints (initial)

| Method | Path | Description |
|--------|------|--------------|
| GET | `/youtube/channel/videos?url=...` | Flat list of videos for a channel/playlist (same shape as `yt-dlp --flat-playlist -j`) |
| GET | `/youtube/video?url=...` | Full video metadata (includes `game`, `game_url`, `game_release_year` when present) |

Example (include the Bearer token):

```bash
curl -H "Authorization: Bearer YOUR_API_SECRET" "http://127.0.0.1:8000/youtube/channel/videos?url=https://www.youtube.com/channel/UCWB7gLoqYpMNewM084K6mFQ/recent"
curl -H "Authorization: Bearer YOUR_API_SECRET" "http://127.0.0.1:8000/youtube/video?url=https://www.youtube.com/watch?v=VIDEO_ID"
```

## Upstream compatibility

The API lives in the `api/` package and does not modify `yt_dlp/`. When you pull from the core yt-dlp repository, only core files change; you can keep the API layer and update it as needed.

## Extensibility

- **More providers (e.g. Twitch)**: Add `api/routes/twitch.py` with prefix `"/twitch"` and routes that validate Twitch URLs and call `service.extract(url, "<type>")`.
- **More data types**: Add a new `extract_type` in `api/service.py` (e.g. `"search"`, `"comments"`), wire the right yt-dlp options, then add a new route under the right provider (e.g. `GET /youtube/search`).
