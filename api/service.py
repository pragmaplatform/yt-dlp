"""Provider-agnostic extraction: build ydl_opts from extract_type and call yt-dlp."""

from __future__ import annotations

import json
import math
import os
import sys
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from typing import Literal
from urllib.parse import urlparse

from yt_dlp import YoutubeDL
from yt_dlp.networking import Request
from yt_dlp.networking.exceptions import TransportError


EXTRACT_TYPES = Literal['playlist_flat', 'video', 'captions', 'chat']

# Warnings that are noise for metadata-only extraction (we never download
# formats, so missing-format / JS-runtime notices don't matter). Matched as
# substrings against the warning text. Anything not listed here — e.g. HTTP 429
# rate-limiting, geo-blocks, real extraction failures — is kept.
_SUPPRESSED_WARNINGS = (
    'No supported JavaScript runtime',
    'YouTube extraction without a JS runtime has been deprecated',
    'have been skipped as they are missing a URL',  # SABR / android_vr format skips
    'SABR-only streaming experiment',
)


class _FilteringLogger:
    """yt-dlp logger that drops known-noise warnings but forwards the rest.

    Setting a `logger` in ydl_opts makes report_warning route here instead of
    honoring `no_warnings` — so we can suppress selectively rather than all or
    nothing. Output is written to stderr with yt-dlp's native prefixes so log
    scraping/grep for `WARNING:` / `ERROR:` keeps working.
    """

    def debug(self, msg: str) -> None:
        # to_screen() (the output `quiet` normally hides) and verbose [debug]
        # lines route here. Drop them — the API runs quiet.
        pass

    def info(self, msg: str) -> None:
        pass

    def warning(self, msg: str) -> None:
        if any(s in msg for s in _SUPPRESSED_WARNINGS):
            return
        sys.stderr.write(f'WARNING: {msg}\n')

    def error(self, msg: str) -> None:
        # msg already carries yt-dlp's "ERROR:" prefix from report_error.
        sys.stderr.write(f'{msg}\n')


YTDLP_LOGGER = _FilteringLogger()


# ---------------------------------------------------------------------------
# HTTP byte-counting instrumentation
# ---------------------------------------------------------------------------

def _url_label(url: str) -> str:
    """Return scheme+host+path of a URL, dropping query params and tokens."""
    try:
        p = urlparse(url)
        return f'{p.scheme}://{p.netloc}{p.path}'
    except Exception:
        return url[:80]


class _CountingResponse:
    """Transparent proxy for a yt-dlp Response that counts bytes via read()."""

    def __init__(self, inner, entry: dict):
        self._inner = inner
        self._entry = entry  # {'url': str, 'bytes': int}

    def read(self, amt=None):
        data = self._inner.read(amt)
        if data:
            self._entry['bytes'] += len(data)
        return data

    def readable(self):
        return self._inner.readable()

    def close(self):
        return self._inner.close()

    @property
    def closed(self):
        return self._inner.closed

    def __getattr__(self, name):
        return getattr(self._inner, name)

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.close()


class _MeasuringYoutubeDL(YoutubeDL):
    """YoutubeDL subclass that records bytes read from every HTTP response."""

    def __init__(self, params):
        self.request_log: list[dict] = []  # [{'url': str, 'bytes': int}]
        super().__init__(params)

    @property
    def total_bytes(self) -> int:
        return sum(r['bytes'] for r in self.request_log)

    def urlopen(self, req):
        resp = super().urlopen(req)
        url = resp.url if hasattr(resp, 'url') else (req if isinstance(req, str) else getattr(req, 'url', '?'))
        entry = {'url': _url_label(url), 'bytes': 0}
        self.request_log.append(entry)
        return _CountingResponse(resp, entry)


# ---------------------------------------------------------------------------
# Extraction helpers
# ---------------------------------------------------------------------------

def _proxy_url() -> str | None:
    """Proxy URL from PROXY_URL env, or None if unset."""
    url = os.environ.get('PROXY_URL', '').strip()
    return url or None


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


def _opts_for(extract_type: EXTRACT_TYPES, url: str = '', limit: int | None = None) -> dict:
    base = {
        'skip_download': True,
        'quiet': True,
        # Route output through a logger that drops noise warnings (JS runtime,
        # skipped formats) but keeps signal like HTTP 429. See _FilteringLogger.
        'logger': YTDLP_LOGGER,
        'ignore_no_formats_error': True,
        'http_headers': {
            'Accept-Encoding': 'gzip, deflate',
        },
    }
    if extract_type == 'playlist_flat':
        base['extract_flat'] = 'in_playlist'
        # Bound playlist extraction so a huge channel can't pull thousands of
        # entries into memory (sanitize_info deep-copies the whole result, then
        # the JSON encoder copies it again — ~3x the payload at peak).
        if limit is not None and limit > 0:
            base['playlistend'] = limit
    if extract_type == 'video':
        # Skip the 1.2 MB /watch webpage. android_vr has REQUIRE_JS_PLAYER=False
        # and _download_ytcfg returns {} immediately (no extra HTTP call), so it
        # calls /player directly. The /next API fallback then supplies engagement
        # data (like_count, comment_count, game panel, etc.). Together ~430 KB vs
        # ~1.25 MB for the webpage path — ~66% savings with no field loss.
        base['extractor_args'] = {
            'youtube': {
                'player_client': ['android_vr'],
                'player_skip': ['webpage'],
            },
        }
    if extract_type == 'captions':
        # android_vr is sometimes bot-checked per video, and web/mweb caption
        # URLs need a PO token; ios/android serve tokenless tracks without the
        # JS player. /next (initial_data) carries nothing caption-related.
        base['extractor_args'] = {
            'youtube': {
                'player_client': ['android_vr', 'ios', 'android'],
                'player_skip': ['webpage', 'initial_data'],
            },
        }
    proxy = _proxy_url()
    if proxy:
        base['proxy'] = proxy
    if 'tiktok.com' in url:
        tiktok_args = _tiktok_extractor_args()
        if tiktok_args:
            base['extractor_args'] = {**(base.get('extractor_args') or {}), **tiktok_args}
    return base


def _debug_enabled() -> bool:
    return os.environ.get('DEBUG', '').strip().lower() in ('1', 'true', 'yes')


def _log_request_summary(label: str, request_log: list[dict]) -> None:
    total = sum(r['bytes'] for r in request_log)
    sys.stderr.write(
        f'METRICS [{label}]: {len(request_log)} requests, '
        f'{total:,} bytes decompressed\n',
    )
    for r in request_log:
        sys.stderr.write(f'  {r["bytes"]:>10,}B  {r["url"]}\n')


def _pick_caption_track(info: dict) -> tuple[str, bool, list[dict]] | None:
    subtitles = info.get('subtitles') or {}
    automatic = info.get('automatic_captions') or {}
    for lang in ('en', 'en-US', 'en-GB'):
        if subtitles.get(lang):
            return lang, False, subtitles[lang]
    if automatic.get('en-orig'):
        return 'en-orig', True, automatic['en-orig']
    orig = next((lang for lang in automatic if lang.endswith('-orig')), None)
    if orig:
        return orig, True, automatic[orig]
    first_manual = next((lang for lang, tracks in subtitles.items() if tracks and lang != 'live_chat'), None)
    if first_manual:
        return first_manual, False, subtitles[first_manual]
    return None


def _json3_to_text(data: dict) -> str:
    lines = []
    for event in data.get('events') or []:
        line = ''.join(seg.get('utf8', '') for seg in event.get('segs') or [])
        line = ' '.join(line.split())
        if line:
            lines.append(line)
    return ' '.join(lines)


def extract_captions(url: str) -> tuple[dict | None, list[dict]]:
    """
    Return the full caption text for a video, preferring English (manual, then
    auto-generated) and falling back to the video's original language.
    """
    opts = _opts_for('captions', url)
    debug = _debug_enabled()
    ydl_class = _MeasuringYoutubeDL if debug else YoutubeDL
    with ydl_class(opts) as ydl:
        info = ydl.extract_info(url, download=False, process=False)
        if info is None:
            return None, list(ydl.request_log) if debug else []
        picked = _pick_caption_track(info)
        result = {'id': info.get('id'), 'title': info.get('title'), 'language': None, 'is_automatic': None, 'text': None}
        if picked:
            lang, is_automatic, tracks = picked
            track = next((t for t in tracks if t.get('ext') == 'json3'), None)
            if track:
                data = json.loads(ydl.urlopen(track['url']).read().decode())
                result.update(language=lang.removesuffix('-orig'), is_automatic=is_automatic, text=_json3_to_text(data))
        request_log = list(ydl.request_log) if debug else []

    if debug:
        _log_request_summary('captions', request_log)
    return result, request_log


_TWITCH_GQL_URL = 'https://gql.twitch.tv/gql'
_TWITCH_CLIENT_ID = 'ue6666qo983tsx6so1t0vnawi233wa'
_TWITCH_CHAT_BATCH_SIZE = 5
_TWITCH_CHAT_WORKERS = 8
_TWITCH_CHAT_ATTEMPTS = 3
_TWITCH_CHAT_MIN_SEGMENT_SECONDS = 30

_TWITCH_VIDEO_QUERY = 'query($id: ID!) { video(id: $id) { id title lengthSeconds owner { login } } }'

_TWITCH_CHAT_QUERY = '''query($id: ID!, $offset: Int) {
  video(id: $id) {
    comments(contentOffsetSeconds: $offset) {
      edges { node {
        id contentOffsetSeconds createdAt
        commenter { id login displayName }
        message { fragments { text emote { emoteID } } userBadges { setID version } userColor }
      } }
      pageInfo { hasNextPage }
    }
  }
}'''


@dataclass
class _ChatSegment:
    offset: int
    end: float
    done: bool = False


def _twitch_gql(ydl: YoutubeDL, operations: list[dict]) -> list[dict]:
    request = Request(
        _TWITCH_GQL_URL,
        data=json.dumps(operations).encode(),
        headers={'Client-ID': _TWITCH_CLIENT_ID, 'Content-Type': 'text/plain;charset=UTF-8'},
    )
    responses = json.loads(ydl.urlopen(request).read().decode())
    if isinstance(responses, dict):
        responses = [responses]
    for response in responses:
        if response.get('errors'):
            raise RuntimeError(f'Twitch GQL error: {response["errors"][0].get("message")}')
    return [response['data'] for response in responses]


def _chat_segments(start: int, end: int | None, length: int) -> list[_ChatSegment]:
    span_end = min(end, length) if end is not None else length
    span = span_end - start
    if span <= 0:
        return [_ChatSegment(offset=start, end=end if end is not None else math.inf)]
    count = min(math.ceil(span / _TWITCH_CHAT_MIN_SEGMENT_SECONDS), _TWITCH_CHAT_WORKERS * _TWITCH_CHAT_BATCH_SIZE)
    boundaries = [start + span * i // count for i in range(count)]
    ends = [*boundaries[1:], end if end is not None else math.inf]
    return [_ChatSegment(offset=s, end=e) for s, e in zip(boundaries, ends)]


def _advance_segment(segment: _ChatSegment, comments: dict) -> None:
    # Twitch rejects cursor paging without a client-integrity token, so we page
    # by offset. A page starts a few seconds before the requested offset and
    # covers whole seconds, so re-requesting at the last offset seen is gap-free.
    edges = comments.get('edges') or []
    has_next = (comments.get('pageInfo') or {}).get('hasNextPage')
    last = edges[-1]['node']['contentOffsetSeconds'] if edges else None
    if not has_next or last is None or last >= segment.end:
        segment.done = True
    else:
        segment.offset = last if last > segment.offset else segment.offset + 1


def _fetch_chat_nodes(ydl: YoutubeDL, video_id: str, segments: list[_ChatSegment], start: int, end: int | None) -> list[dict]:
    def fetch(batch: list[_ChatSegment]) -> list[dict]:
        operations = [
            {'query': _TWITCH_CHAT_QUERY, 'variables': {'id': video_id, 'offset': segment.offset}}
            for segment in batch
        ]
        for attempt in range(1, _TWITCH_CHAT_ATTEMPTS + 1):
            try:
                return _twitch_gql(ydl, operations)
            except TransportError:
                if attempt == _TWITCH_CHAT_ATTEMPTS:
                    raise

    nodes: dict[str, dict] = {}
    with ThreadPoolExecutor(_TWITCH_CHAT_WORKERS) as pool:
        while active := [s for s in segments if not s.done]:
            batches = [active[i:i + _TWITCH_CHAT_BATCH_SIZE] for i in range(0, len(active), _TWITCH_CHAT_BATCH_SIZE)]
            for batch, results in zip(batches, pool.map(fetch, batches)):
                for segment, data in zip(batch, results):
                    comments = ((data.get('video') or {}).get('comments')) or {}
                    for edge in comments.get('edges') or []:
                        node = edge['node']
                        if node['contentOffsetSeconds'] >= start and (end is None or node['contentOffsetSeconds'] < end):
                            nodes[node['id']] = node
                    _advance_segment(segment, comments)
    return sorted(nodes.values(), key=lambda n: (n['contentOffsetSeconds'], n.get('createdAt') or ''))


def _format_chat_message(node: dict) -> dict:
    message = node.get('message') or {}
    fragments = message.get('fragments') or []
    commenter = node.get('commenter')
    return {
        'id': node['id'],
        'offset_seconds': node['contentOffsetSeconds'],
        'created_at': node.get('createdAt'),
        'author': {
            'id': commenter.get('id'),
            'login': commenter.get('login'),
            'display_name': commenter.get('displayName'),
        } if commenter else None,
        'text': ''.join(fragment.get('text') or '' for fragment in fragments),
        'emotes': [
            {'id': fragment['emote']['emoteID'], 'text': fragment.get('text')}
            for fragment in fragments if fragment.get('emote')
        ],
        'badges': [
            {'set_id': badge['setID'], 'version': badge.get('version')}
            for badge in message.get('userBadges') or [] if badge.get('setID')
        ],
        'color': message.get('userColor'),
    }


def extract_twitch_chat(video_id: str, start: int = 0, end: int | None = None) -> tuple[dict | None, list[dict]]:
    """Return the chat replay for a Twitch VOD, optionally limited to [start, end) seconds."""
    debug = _debug_enabled()
    ydl_class = _MeasuringYoutubeDL if debug else YoutubeDL
    with ydl_class(_opts_for('chat')) as ydl:
        [video_data] = _twitch_gql(ydl, [{'query': _TWITCH_VIDEO_QUERY, 'variables': {'id': video_id}}])
        video = video_data.get('video')
        if video is None:
            return None, list(ydl.request_log) if debug else []
        segments = _chat_segments(start, end, video.get('lengthSeconds') or 0)
        nodes = _fetch_chat_nodes(ydl, video_id, segments, start, end)
        request_log = list(ydl.request_log) if debug else []

    if debug:
        _log_request_summary('chat', request_log)
    return {
        'id': video['id'],
        'title': video.get('title'),
        'channel': (video.get('owner') or {}).get('login'),
        'duration': video.get('lengthSeconds'),
        'start': start,
        'end': end,
        'message_count': len(nodes),
        'messages': [_format_chat_message(node) for node in nodes],
    }, request_log


def extract(url: str, extract_type: EXTRACT_TYPES, limit: int | None = None) -> tuple[dict | None, list[dict]]:
    """
    Extract metadata for the given URL. Returns (info_dict, request_log).

    When DEBUG=true, uses _MeasuringYoutubeDL to record per-request byte counts
    and logs a summary to stderr. In production (DEBUG unset) the standard
    YoutubeDL is used and request_log is always empty.

    `limit` caps entries for playlist_flat extraction.
    """
    opts = _opts_for(extract_type, url, limit)
    debug = _debug_enabled()
    ydl_class = _MeasuringYoutubeDL if debug else YoutubeDL
    with ydl_class(opts) as ydl:
        result = ydl.extract_info(url, download=False)
        request_log = list(ydl.request_log) if debug else []

    if debug:
        _log_request_summary(extract_type, request_log)

    if result is None:
        return None, request_log
    # remove_private_keys=True would strip 'entries' from playlists; keep it so channel/videos returns the list
    return YoutubeDL.sanitize_info(result, remove_private_keys=False), request_log
