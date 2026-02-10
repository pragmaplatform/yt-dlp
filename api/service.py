"""Provider-agnostic extraction: build ydl_opts from extract_type and call yt-dlp."""

from __future__ import annotations

import os
from typing import Literal
from urllib.parse import quote

from yt_dlp import YoutubeDL


EXTRACT_TYPES = Literal['playlist_flat', 'video']

APIFY_PROXY_HOST = 'proxy.apify.com'
APIFY_PROXY_PORT = 8000
APIFY_RESIDENTIAL_USER = 'groups-RESIDENTIAL'


def _apify_proxy_url() -> str | None:
    """Build Apify residential proxy URL if PROXY_APIFY_PASSWORD is set."""
    password = os.environ.get('PROXY_APIFY_PASSWORD')
    if not password:
        return None
    encoded = quote(password, safe='')
    return f'http://{APIFY_RESIDENTIAL_USER}:{encoded}@{APIFY_PROXY_HOST}:{APIFY_PROXY_PORT}'


def _tiktok_extractor_args() -> dict | None:
    """TikTok extractor args (device_id + app_info) when TIKTOK_DEVICE_ID is set. Required for hashtag posts (mobile API)."""
    device_id = os.environ.get('TIKTOK_DEVICE_ID', '').strip()
    if not device_id:
        return None
    return {
        'tiktok': {
            'device_id': [device_id],
            'app_info': [''],  # use extractor defaults; device_id alone enables mobile API
        },
    }


def _opts_for(extract_type: EXTRACT_TYPES, url: str = '') -> dict:
    base = {
        'skip_download': True,
        'quiet': True,
        'ignore_no_formats_error': True,
    }
    if extract_type == 'playlist_flat':
        base['extract_flat'] = 'in_playlist'
    proxy = _apify_proxy_url()
    if proxy:
        base['proxy'] = proxy
    if 'tiktok.com' in url:
        tiktok_args = _tiktok_extractor_args()
        if tiktok_args:
            base['extractor_args'] = {**(base.get('extractor_args') or {}), **tiktok_args}
    return base


def extract(url: str, extract_type: EXTRACT_TYPES) -> dict | None:
    """
    Extract metadata for the given URL. No provider-specific logic;
    yt-dlp selects the extractor from the URL.
    """
    opts = _opts_for(extract_type, url)
    with YoutubeDL(opts) as ydl:
        result = ydl.extract_info(url, download=False)
    if result is None:
        return None
    # remove_private_keys=True would strip 'entries' from playlists; keep it so channel/videos returns the list
    return YoutubeDL.sanitize_info(result, remove_private_keys=False)
