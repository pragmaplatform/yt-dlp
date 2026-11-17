"""Twitch provider routes."""

from urllib.parse import urlparse

from fastapi import APIRouter, HTTPException, Query, Response

from api import service
from yt_dlp.extractor.twitch import TwitchVodIE

router = APIRouter()


def _set_metrics_headers(response: Response, request_log: list[dict]) -> None:
    total = sum(r['bytes'] for r in request_log)
    response.headers['X-Requests'] = str(len(request_log))
    response.headers['X-Bytes-Decompressed'] = str(total)


def _is_twitch_url(url: str) -> bool:
    try:
        parsed = urlparse(url)
        netloc = parsed.netloc.lower().removeprefix('www.')
        return netloc == 'twitch.tv' or netloc.endswith('.twitch.tv')
    except Exception:
        return False


@router.get('/video')
def video(url: str = Query(..., description='Twitch video URL (e.g. .../videos/ID)'), response: Response = None):
    """Return full video metadata for a Twitch VOD."""
    if not _is_twitch_url(url):
        raise HTTPException(status_code=400, detail='URL must be a Twitch video URL')
    try:
        result, request_log = service.extract(url, 'video')
    except Exception as e:
        raise HTTPException(status_code=502, detail=str(e)) from e
    if result is None:
        raise HTTPException(status_code=404, detail='No data extracted')
    if response is not None:
        _set_metrics_headers(response, request_log)
    return result


@router.get('/chat')
def chat(
    url: str = Query(..., description='Twitch video URL (e.g. .../videos/ID)'),
    start: int = Query(0, ge=0, description='Only messages at or after this many seconds into the VOD'),
    end: int | None = Query(None, ge=1, description='Only messages before this many seconds into the VOD'),
    response: Response = None,
):
    """Return the chat replay (messages sent during the stream) for a Twitch VOD."""
    video_id = TwitchVodIE.get_temp_id(url)
    if video_id is None:
        raise HTTPException(status_code=400, detail='URL must be a Twitch video URL')
    if end is not None and end <= start:
        raise HTTPException(status_code=400, detail='end must be greater than start')
    try:
        result, request_log = service.extract_twitch_chat(video_id, start, end)
    except Exception as e:
        raise HTTPException(status_code=502, detail=str(e)) from e
    if result is None:
        raise HTTPException(status_code=404, detail='Video not found')
    if response is not None:
        _set_metrics_headers(response, request_log)
    return result
