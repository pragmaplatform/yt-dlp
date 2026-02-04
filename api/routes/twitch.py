"""Twitch provider routes."""

from urllib.parse import urlparse

from fastapi import APIRouter, HTTPException, Query

from api import service

router = APIRouter()


def _is_twitch_url(url: str) -> bool:
    try:
        parsed = urlparse(url)
        netloc = parsed.netloc.lower().lstrip('www.')
        return netloc == 'twitch.tv' or netloc.endswith('.twitch.tv')
    except Exception:
        return False


@router.get('/video')
def video(url: str = Query(..., description='Twitch video URL (e.g. .../videos/ID)')):
    """Return full video metadata for a Twitch VOD."""
    if not _is_twitch_url(url):
        raise HTTPException(status_code=400, detail='URL must be a Twitch video URL')
    try:
        result = service.extract(url, 'video')
    except Exception as e:
        raise HTTPException(status_code=502, detail=str(e)) from e
    if result is None:
        raise HTTPException(status_code=404, detail='No data extracted')
    return result
