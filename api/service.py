"""Provider-agnostic extraction: build ydl_opts from extract_type and call yt-dlp."""

from __future__ import annotations

from typing import Literal

from yt_dlp import YoutubeDL


EXTRACT_TYPES = Literal['playlist_flat', 'video']


def _opts_for(extract_type: EXTRACT_TYPES) -> dict:
    base = {'skip_download': True, 'quiet': True}
    if extract_type == 'playlist_flat':
        base['extract_flat'] = 'in_playlist'
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
