"""Twitter/X provider routes.

Endpoints:
- GET /twitter/tweet      — metadata for a single tweet by URL
- GET /twitter/user/posts — flat list of recent tweets for a public user

Authentication:
Individual tweets work without auth via yt-dlp's TwitterIE (uses X's guest
token flow). User timelines are extracted from the SSR (React Server Components)
payload embedded in X's public profile page — no auth required.

If TWITTER_AUTH_TOKEN + TWITTER_CT0 are set they are injected as cookies
(which may improve rate limits or unlock non-public content), but they are
not required for public profiles.
"""

from __future__ import annotations

import base64
import re
from typing import Any
from urllib.parse import urlparse

from fastapi import APIRouter, HTTPException, Query, Response
from yt_dlp import YoutubeDL

from api import service

router = APIRouter()

_TWITTER_HOSTS = {
    'twitter.com', 'www.twitter.com', 'mobile.twitter.com',
    'x.com', 'www.x.com', 'mobile.x.com',
}

_B64_TWEET = r'VHdlZXQ6[A-Za-z0-9+/=]+'


def _is_twitter_url(url: str) -> bool:
    try:
        return urlparse(url).netloc.lower() in _TWITTER_HOSTS
    except Exception:
        return False


def _set_metrics_headers(response: Response, request_log: list[dict]) -> None:
    total = sum(r['bytes'] for r in request_log)
    response.headers['X-Requests'] = str(len(request_log))
    response.headers['X-Bytes-Decompressed'] = str(total)


def _twitter_ydl_opts() -> dict:
    opts = {
        'skip_download': True,
        'quiet': True,
        'logger': service.YTDLP_LOGGER,
        'ignore_no_formats_error': True,
    }
    proxy = service._proxy_url()
    if proxy:
        opts['proxy'] = proxy
    return opts


def _inject_auth(ie) -> bool:
    """Set auth_token / ct0 cookies from env vars if available. Returns True if injected."""
    import os
    auth_token = os.environ.get('TWITTER_AUTH_TOKEN', '').strip()
    ct0 = os.environ.get('TWITTER_CT0', '').strip()
    if not auth_token:
        return False
    for domain in ('x.com', 'api.x.com', 'twitter.com', 'api.twitter.com'):
        ie._set_cookie(domain, 'auth_token', auth_token)
        if ct0:
            ie._set_cookie(domain, 'ct0', ct0)
    return True


def _unescape_js(s: str) -> str:
    """Unescape JavaScript string escapes found in the RSC page payload."""
    i, result = 0, []
    while i < len(s):
        if s[i] == '\\' and i + 1 < len(s):
            c = s[i + 1]
            if c == 'n':
                result.append('\n'); i += 2
            elif c == 't':
                result.append('\t'); i += 2
            elif c == 'r':
                result.append('\r'); i += 2
            elif c == '"':
                result.append('"'); i += 2
            elif c == "'":
                result.append("'"); i += 2
            elif c == '\\':
                result.append('\\'); i += 2
            elif c == '/':
                result.append('/'); i += 2
            elif c == 'u' and i + 5 <= len(s):
                try:
                    result.append(chr(int(s[i + 2:i + 6], 16)))
                    i += 6
                except ValueError:
                    result.append(s[i]); i += 1
            else:
                result.append(s[i]); i += 1
        else:
            result.append(s[i]); i += 1
    return ''.join(result)


def _parse_rsc_tweets(page: str, username: str) -> list[dict]:
    """Extract tweet data from the RSC payload embedded in X's SSR profile page.

    X server-renders timeline tweets into a <script> block using the React Server
    Components flight format. Each tweet's counts, text, timestamp, and media are
    stored as flat key-value pairs keyed by base64(Tweet:{id}).

    The RSC payload also embeds the original tweets of every retweet/quote plus
    X recommendation content, which would pollute the results with old unrelated
    tweets. We filter to only the tweet IDs that appear as primary
    TimelineTimelineEntry items — the entries X actually chose to show on the
    profile timeline.
    """
    scripts = re.findall(r'<script[^>]+>(.*?)</script>', page, re.DOTALL)
    if not scripts:
        return []
    big = max(scripts, key=len)

    # Collect the ordered set of primary timeline tweet IDs.
    # TimelineTimelineEntry:tweet-{id} appears only for direct timeline items,
    # not for embedded retweet originals or quoted tweets.
    seen: set[str] = set()
    timeline_ids: list[str] = []
    for m in re.finditer(r'TimelineTimelineEntry:tweet-(\d+)', big):
        tid = m.group(1)
        if tid not in seen:
            seen.add(tid)
            timeline_ids.append(tid)

    # Fall back to all tweet IDs if the timeline marker is missing.
    use_filter = bool(timeline_ids)

    # Extract user display name from UserCore block
    user_name = ''
    user_m = re.search(r'__typename:"UserCore",name:"([^"]+)",screen_name:"' + re.escape(username) + r'"', big, re.IGNORECASE)
    if user_m:
        user_name = user_m.group(1)

    def _decode_tweet_id(b64: str) -> str | None:
        try:
            return base64.b64decode(b64).decode('utf-8').split(':', 1)[1]
        except Exception:
            return None

    # Counts: reply_count, favorite_count, retweet_count from ApiCounts blocks
    counts: dict[str, dict] = {}
    counts_pat = re.compile(
        r'__id:"client:(' + _B64_TWEET + r'):counts"[^}]*?'
        r'reply_count:(\d+)[^}]*?favorite_count:(\d+)[^}]*?retweet_count:(\d+)',
        re.DOTALL,
    )
    for m in counts_pat.finditer(big):
        tweet_id = _decode_tweet_id(m.group(1))
        if tweet_id:
            counts[tweet_id] = {
                'comment_count': int(m.group(2)),
                'like_count': int(m.group(3)),
                'repost_count': int(m.group(4)),
            }

    # Details: full_text + created_at_ms from TBirdData blocks
    details: dict[str, dict] = {}
    detail_pat = re.compile(
        r'__id:"client:(' + _B64_TWEET + r'):details".*?'
        r'full_text:"((?:[^"\\]|\\.)*)".*?'
        r'created_at_ms:(\d+)',
        re.DOTALL,
    )
    for m in detail_pat.finditer(big):
        tweet_id = _decode_tweet_id(m.group(1))
        if tweet_id:
            details[tweet_id] = {
                'text': _unescape_js(m.group(2)),
                'timestamp': int(m.group(3)) // 1000,
            }

    # View counts from ViewCountInfo blocks (count is a quoted string or null)
    views: dict[str, int] = {}
    views_pat = re.compile(
        r'__id:"client:(' + _B64_TWEET + r'):views"[^}]*?'
        r'__typename:"ViewCountInfo",count:("[\d]+"|\d+|null)',
    )
    for m in views_pat.finditer(big):
        tweet_id = _decode_tweet_id(m.group(1))
        raw = m.group(2).strip('"')
        if tweet_id and raw != 'null':
            try:
                views[tweet_id] = int(raw)
            except ValueError:
                pass

    # Media: first thumbnail per tweet via media_entities2 client key
    media: dict[str, str] = {}
    media_pat = re.compile(
        r'__id:"client:(' + _B64_TWEET + r'):media_entities2:\d+"[^}]*?media_url_https:"([^"]+)"'
    )
    for m in media_pat.finditer(big):
        tweet_id = _decode_tweet_id(m.group(1))
        if tweet_id and tweet_id not in media:
            media[tweet_id] = m.group(2)

    candidate_ids = timeline_ids if use_filter else list(set(details) | set(counts))

    tweets = []
    for tweet_id in candidate_ids:
        if tweet_id not in details and tweet_id not in counts:
            continue
        t: dict[str, Any] = {
            'id': tweet_id,
            'text': '',
            'timestamp': 0,
            'like_count': None,
            'repost_count': None,
            'comment_count': None,
            'view_count': views.get(tweet_id),
            'thumbnail': media.get(tweet_id),
            'uploader': user_name,
            'uploader_id': username,
            'uploader_url': f'https://x.com/{username}',
            'webpage_url': f'https://x.com/{username}/status/{tweet_id}',
            'tags': [],
            'age_limit': 0,
        }
        if tweet_id in details:
            t.update(details[tweet_id])
        if tweet_id in counts:
            t.update(counts[tweet_id])
        t['tags'] = re.findall(r'#(\w+)', t.get('text', ''))
        tweets.append(t)

    tweets.sort(key=lambda x: x['timestamp'], reverse=True)
    return tweets


def _entry_to_tweet(entry: dict[str, Any]) -> dict[str, Any]:
    """Normalise a yt-dlp info_dict to our tweet shape."""
    thumbs = entry.get('thumbnails') or []
    thumb_url = thumbs[-1].get('url') if thumbs and isinstance(thumbs[-1], dict) else None
    return {
        'id': entry.get('id') or entry.get('display_id') or '',
        'text': entry.get('description') or '',
        'timestamp': entry.get('timestamp') or 0,
        'like_count': entry.get('like_count'),
        'repost_count': entry.get('repost_count'),
        'comment_count': entry.get('comment_count'),
        'view_count': entry.get('view_count'),
        'thumbnail': thumb_url or entry.get('thumbnail'),
        'uploader': entry.get('uploader') or '',
        'uploader_id': entry.get('uploader_id') or '',
        'uploader_url': entry.get('uploader_url') or '',
        'webpage_url': entry.get('webpage_url') or '',
        'tags': entry.get('tags') or [],
        'age_limit': entry.get('age_limit') or 0,
    }


def _fetch_tweet(url: str) -> tuple[dict | None, list[dict]]:
    """Extract tweet metadata via yt-dlp's TwitterIE (works without auth)."""
    debug = service._debug_enabled()
    ydl_class = service._MeasuringYoutubeDL if debug else YoutubeDL

    with ydl_class(_twitter_ydl_opts()) as ydl:
        from yt_dlp.extractor.twitter import TwitterIE
        ie = TwitterIE(ydl)
        _inject_auth(ie)

        try:
            result = ydl.extract_info(url, download=False)
        except Exception as e:
            raise RuntimeError(str(e)) from e

        request_log = list(ydl.request_log) if debug else []

    if debug:
        service._log_request_summary('twitter:tweet', request_log)

    result = YoutubeDL.sanitize_info(result, remove_private_keys=False) if result else None
    return result, request_log


def _fetch_user_tweets(username: str) -> tuple[list[dict], list[dict]]:
    """Fetch a user's recent tweets by parsing X's SSR profile page.

    Works without authentication. X embeds the first ~20-30 tweets in a
    <script> block (React Server Components payload) on every public profile
    page. Auth cookies are injected when available but are not required.
    """
    debug = service._debug_enabled()
    ydl_class = service._MeasuringYoutubeDL if debug else YoutubeDL

    with ydl_class(_twitter_ydl_opts()) as ydl:
        from yt_dlp.extractor.twitter import TwitterIE
        ie = TwitterIE(ydl)
        _inject_auth(ie)

        page = ie._download_webpage(
            f'https://x.com/{username}',
            username,
            fatal=False,
            impersonate=True,
            note=f'Fetching X profile page for @{username}',
            errnote=f'Failed to fetch X profile page for @{username}',
        )

        request_log = list(ydl.request_log) if debug else []

    if debug:
        service._log_request_summary('twitter:user', request_log)

    if not page:
        return [], request_log

    tweets = _parse_rsc_tweets(page, username)
    return tweets, request_log


@router.get('/tweet')
def tweet(
    url: str = Query(..., description='Tweet URL (e.g. https://x.com/user/status/ID)'),
    response: Response = None,
):
    """Return metadata for a single tweet (no auth required for public tweets)."""
    if not _is_twitter_url(url):
        raise HTTPException(status_code=400, detail='URL must be a Twitter/X tweet URL')
    try:
        result, request_log = _fetch_tweet(url)
    except Exception as e:
        raise HTTPException(status_code=502, detail=str(e)) from e
    if result is None:
        raise HTTPException(status_code=404, detail='No data extracted')
    if response is not None:
        _set_metrics_headers(response, request_log)
    return _entry_to_tweet(result)


@router.get('/user/posts')
def user_posts(
    username: str = Query(..., description='Twitter/X username (without @)'),
    count: int = Query(30, ge=1, le=200, description='Max number of tweets to return'),
    response: Response = None,
):
    """Return recent tweets for a public Twitter/X user (no auth required).

    Extracts tweets from the RSC payload embedded in X's SSR profile page.
    Returns the first ~20-30 tweets visible on the profile page. Set
    TWITTER_AUTH_TOKEN + TWITTER_CT0 in the environment to inject auth cookies
    (may improve rate limits) but they are not required.
    """
    username = username.strip().lstrip('@')
    if not username:
        raise HTTPException(status_code=400, detail='username is required')

    try:
        tweets, request_log = _fetch_user_tweets(username)
    except Exception as e:
        raise HTTPException(status_code=502, detail=str(e)) from e

    if response is not None:
        _set_metrics_headers(response, request_log)

    return {
        'username': username,
        'tweets': tweets[:count],
    }
