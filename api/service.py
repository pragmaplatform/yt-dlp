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


def _opts_for(extract_type: EXTRACT_TYPES) -> dict:
    base = {'skip_download': True, 'quiet': True}
    if extract_type == 'playlist_flat':
        base['extract_flat'] = 'in_playlist'
    proxy = _apify_proxy_url()
    if proxy:
        base['proxy'] = proxy
    return base


def extract(url: str, extract_type: EXTRACT_TYPES) -> dict | None:
    """
    Extract metadata for the given URL. No provider-specific logic;
    yt-dlp selects the extractor from the URL.
    """
    opts = _opts_for(extract_type)
    with YoutubeDL(opts) as ydl:
        result = ydl.extract_info(url, download=False)
    if result is None:
        return None
    # remove_private_keys=True would strip 'entries' from playlists; keep it so channel/videos returns the list
    return YoutubeDL.sanitize_info(result, remove_private_keys=False)
