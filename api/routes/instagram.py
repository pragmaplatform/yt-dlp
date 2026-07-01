"""Instagram provider routes.

Endpoints:
- GET /instagram/post         — single post or reel by URL
- GET /instagram/user/posts   — flat list of posts for a public user
- GET /instagram/user/stories — current active stories for a user (requires INSTAGRAM_SESSION_ID)

Note: InstagramUserIE in yt-dlp is marked _WORKING=False because its old
GraphQL endpoint (/graphql/query/ with query_hash) is dead. We bypass it and
call Instagram's mobile API (i.instagram.com/api/v1/users/web_profile_info/)
directly, using yt-dlp's HTTP infrastructure for proxy/cookie support.
For private profiles or when rate-limited, set INSTAGRAM_SESSION_ID in the
environment with a valid logged-in sessionid cookie value.
"""

from __future__ import annotations

import os
from typing import Any
from urllib.parse import urlparse

from fastapi import APIRouter, HTTPException, Query, Response
from yt_dlp import YoutubeDL

from api import service

router = APIRouter()

_INSTAGRAM_HOSTS = {'instagram.com', 'www.instagram.com'}
_IG_API_BASE = 'https://i.instagram.com/api/v1'
_IG_APP_ID = '936619743392459'

_IG_BASE_HEADERS = {
    'X-IG-App-ID': _IG_APP_ID,
    'X-ASBD-ID': '198387',
    'X-IG-WWW-Claim': '0',
    'Origin': 'https://www.instagram.com',
    'Accept': '*/*',
}


def _is_instagram_url(url: str) -> bool:
    try:
        return urlparse(url).netloc.lower() in _INSTAGRAM_HOSTS
    except Exception:
        return False


def _set_metrics_headers(response: Response, request_log: list[dict]) -> None:
    total = sum(r['bytes'] for r in request_log)
    response.headers['X-Requests'] = str(len(request_log))
    response.headers['X-Bytes-Decompressed'] = str(total)


def _ig_ydl_opts() -> dict:
    opts = {
        'skip_download': True,
        'quiet': True,
        'logger': service.YTDLP_LOGGER,
    }
    proxy = service._proxy_url()
    if proxy:
        opts['proxy'] = proxy
    return opts


def _fetch_user_profile(username: str) -> tuple[dict | None, list[dict]]:
    """Fetch user profile + recent posts via Instagram's mobile API.

    Returns (data, request_log) matching the service.extract() contract so
    callers can forward byte-count metrics as response headers.

    InstagramUserIE._WORKING=False (old GraphQL/query_hash endpoint is dead),
    so we skip service.extract() and call web_profile_info directly.

    Strategy mirrors InstagramIE._real_extract for single posts:
    1. Call get_ruling_for_content to establish session cookies
       (ig_did, mid, csrftoken) — same warm-up Instagram's own extractor uses.
    2. Pass the csrftoken in X-CSRFToken on the real API call.
    Both steps use impersonate=True; install the curl-cffi extra so yt-dlp
    can provide a real browser TLS fingerprint (HTTP/2, correct cipher suites),
    which is what prevents the 429 from Instagram.
    """
    debug = service._debug_enabled()
    ydl_class = service._MeasuringYoutubeDL if debug else YoutubeDL

    with ydl_class(_ig_ydl_opts()) as ydl:
        from yt_dlp.extractor.instagram import InstagramIE
        ie = InstagramIE(ydl)

        session_id = os.environ.get('INSTAGRAM_SESSION_ID', '').strip()
        if session_id:
            ie._set_cookie('instagram.com', 'sessionid', session_id)

        # Step 1: session warm-up — exactly what InstagramIE does before its
        # GraphQL call. Sets ig_did / mid / csrftoken cookies.
        ie._download_json(
            f'{_IG_API_BASE}/web/get_ruling_for_content/',
            username,
            query={'content_type': 'MEDIA', 'target_id': '0'},
            headers=_IG_BASE_HEADERS,
            fatal=False,
            impersonate=True,
            note='Setting up Instagram session',
            errnote=False,
        )

        csrf = ie._get_cookies('https://www.instagram.com').get('csrftoken')
        csrf_value = csrf.value if csrf else ''

        # Step 2: fetch profile data from the mobile API.
        data = ie._download_json(
            f'{_IG_API_BASE}/users/web_profile_info/',
            username,
            query={'username': username},
            headers={
                **_IG_BASE_HEADERS,
                'X-Requested-With': 'XMLHttpRequest',
                'X-CSRFToken': csrf_value,
                'Referer': f'https://www.instagram.com/{username}/',
                'Accept-Language': 'en-US,en;q=0.9',
            },
            fatal=False,
            impersonate=True,
            note='Fetching Instagram user profile',
            errnote='Instagram user profile API call failed',
        )

        request_log = list(ydl.request_log) if debug else []

    if debug:
        service._log_request_summary('instagram:user', request_log)

    return data, request_log


def _node_to_post(node: dict[str, Any]) -> dict[str, Any]:
    """Map a GraphQL edge node from edge_owner_to_timeline_media to our post shape."""
    caption_edges = ((node.get('edge_media_to_caption') or {}).get('edges') or [])
    description = ''
    if caption_edges and isinstance(caption_edges[0], dict):
        description = ((caption_edges[0].get('node') or {}).get('text') or '')
    shortcode = node.get('shortcode') or ''
    return {
        'id': node.get('id') or '',
        'shortcode': shortcode,
        'description': description,
        'timestamp': node.get('taken_at_timestamp') or 0,
        'like_count': (node.get('edge_liked_by') or {}).get('count'),
        'comment_count': (node.get('edge_media_to_comment') or {}).get('count'),
        'view_count': node.get('video_view_count'),
        'duration': node.get('video_duration'),
        'thumbnail': node.get('display_url') or node.get('thumbnail_src'),
        'is_video': node.get('is_video', False),
        'webpage_url': f'https://www.instagram.com/p/{shortcode}/' if shortcode else '',
    }


def _entry_to_post(entry: dict[str, Any]) -> dict[str, Any]:
    """Normalise a yt-dlp info_dict to our post shape."""
    thumbs = entry.get('thumbnails') or []
    thumb_url = thumbs[-1].get('url') if thumbs and isinstance(thumbs[-1], dict) else None
    return {
        'id': entry.get('id') or '',
        'shortcode': entry.get('webpage_url', '').rstrip('/').split('/')[-1],
        'description': entry.get('description') or entry.get('title') or '',
        'timestamp': entry.get('timestamp') or 0,
        'like_count': entry.get('like_count'),
        'comment_count': entry.get('comment_count'),
        'view_count': entry.get('view_count'),
        'duration': entry.get('duration'),
        'thumbnail': thumb_url or entry.get('thumbnail'),
        'uploader': entry.get('uploader') or '',
        'uploader_id': entry.get('uploader_id') or '',
        'uploader_url': entry.get('uploader_url') or '',
        'webpage_url': entry.get('webpage_url') or '',
    }


def _fetch_user_stories(username: str) -> tuple[dict | None, str | None, list[dict]]:
    """Fetch current active stories for a user via Instagram's mobile API.

    Returns (raw_reels_response, user_id, request_log).
    Stories are login-gated — set INSTAGRAM_SESSION_ID in the environment or
    this will return an empty reels dict.
    """
    debug = service._debug_enabled()
    ydl_class = service._MeasuringYoutubeDL if debug else YoutubeDL

    with ydl_class(_ig_ydl_opts()) as ydl:
        from yt_dlp.extractor.instagram import InstagramIE
        ie = InstagramIE(ydl)

        session_id = os.environ.get('INSTAGRAM_SESSION_ID', '').strip()
        if session_id:
            ie._set_cookie('instagram.com', 'sessionid', session_id)

        ie._download_json(
            f'{_IG_API_BASE}/web/get_ruling_for_content/',
            username,
            query={'content_type': 'MEDIA', 'target_id': '0'},
            headers=_IG_BASE_HEADERS,
            fatal=False,
            impersonate=True,
            note='Setting up Instagram session',
            errnote=False,
        )

        csrf = ie._get_cookies('https://www.instagram.com').get('csrftoken')
        csrf_value = csrf.value if csrf else ''
        authed_headers = {
            **_IG_BASE_HEADERS,
            'X-Requested-With': 'XMLHttpRequest',
            'X-CSRFToken': csrf_value,
            'Referer': f'https://www.instagram.com/{username}/',
            'Accept-Language': 'en-US,en;q=0.9',
        }

        # Get user_id from web_profile_info so we can key the reels request.
        profile_data = ie._download_json(
            f'{_IG_API_BASE}/users/web_profile_info/',
            username,
            query={'username': username},
            headers=authed_headers,
            fatal=False,
            impersonate=True,
            note='Fetching Instagram user profile',
            errnote='Instagram user profile API call failed',
        )

        user_id = (((profile_data or {}).get('data') or {}).get('user') or {}).get('id')
        if not user_id:
            request_log = list(ydl.request_log) if debug else []
            if debug:
                service._log_request_summary('instagram:stories', request_log)
            return None, None, request_log

        stories_data = ie._download_json(
            f'{_IG_API_BASE}/feed/reels_media/',
            username,
            query={'reel_ids': user_id},
            headers=authed_headers,
            fatal=False,
            impersonate=True,
            note='Fetching Instagram stories',
            errnote='Instagram stories API call failed',
        )

        request_log = list(ydl.request_log) if debug else []

    if debug:
        service._log_request_summary('instagram:stories', request_log)

    return stories_data, user_id, request_log


def _story_item_to_story(item: dict[str, Any]) -> dict[str, Any]:
    """Map a story item from /feed/reels_media/ to our story shape."""
    video_versions = item.get('video_versions') or []
    candidates = ((item.get('image_versions2') or {}).get('candidates') or [])
    return {
        'id': str(item.get('pk') or item.get('id') or ''),
        'media_type': 'video' if item.get('media_type') == 2 else 'photo',
        'timestamp': item.get('taken_at'),
        'expiring_at': item.get('expiring_at'),
        'duration': item.get('video_duration'),
        'thumbnail': candidates[0].get('url') if candidates else None,
        'video_url': video_versions[0].get('url') if video_versions else None,
        'width': (video_versions[0] if video_versions else (candidates[0] if candidates else {})).get('width'),
        'height': (video_versions[0] if video_versions else (candidates[0] if candidates else {})).get('height'),
    }


@router.get('/post')
def post(
    url: str = Query(..., description='Instagram post or reel URL (e.g. https://www.instagram.com/p/SHORTCODE/)'),
    response: Response = None,
):
    """Return metadata for a single Instagram post or reel."""
    if not _is_instagram_url(url):
        raise HTTPException(status_code=400, detail='URL must be an Instagram post or reel URL')
    try:
        result, request_log = service.extract(url, 'video')
    except Exception as e:
        raise HTTPException(status_code=502, detail=str(e)) from e
    if result is None:
        raise HTTPException(status_code=404, detail='No data extracted')
    if response is not None:
        _set_metrics_headers(response, request_log)
    return _entry_to_post(result)


@router.get('/user/posts')
def user_posts(
    username: str = Query(..., description='Instagram username (without @)'),
    count: int = Query(30, ge=1, le=100, description='Max number of posts to return'),
    response: Response = None,
):
    """Return a flat list of recent posts for a public Instagram user.

    Uses Instagram's mobile API directly — yt-dlp's InstagramUserIE is broken
    (marked _WORKING=False). Set INSTAGRAM_SESSION_ID env var with a valid
    sessionid cookie to access private profiles or bypass rate limits.
    """
    username = username.strip().lstrip('@')
    if not username:
        raise HTTPException(status_code=400, detail='username is required')

    try:
        data, request_log = _fetch_user_profile(username)
    except Exception as e:
        raise HTTPException(status_code=502, detail=str(e)) from e

    if not data:
        raise HTTPException(
            status_code=503,
            detail=(
                'Instagram user profile API returned no data. '
                'The account may be private, rate-limited, or login required. '
                'Set INSTAGRAM_SESSION_ID in the environment with a valid sessionid cookie value.'
            ),
        )

    user = (data.get('data') or {}).get('user') or {}
    if not user:
        raise HTTPException(status_code=404, detail=f'User @{username} not found or profile is private')

    if response is not None:
        _set_metrics_headers(response, request_log)

    edges = ((user.get('edge_owner_to_timeline_media') or {}).get('edges') or [])
    posts = [
        _node_to_post(e['node'])
        for e in edges
        if isinstance(e, dict) and isinstance(e.get('node'), dict)
    ][:count]

    return {
        'username': username,
        'user_id': user.get('id'),
        'full_name': user.get('full_name'),
        'biography': user.get('biography'),
        'follower_count': (user.get('edge_followed_by') or {}).get('count'),
        'posts': posts,
    }


@router.get('/user/stories')
def user_stories(
    username: str = Query(..., description='Instagram username (without @)'),
    response: Response = None,
):
    """Return the current active stories for an Instagram user.

    Stories are login-gated by Instagram — set INSTAGRAM_SESSION_ID in the
    environment with a valid sessionid cookie value, otherwise this returns 401.
    """
    username = username.strip().lstrip('@')
    if not username:
        raise HTTPException(status_code=400, detail='username is required')

    if not os.environ.get('INSTAGRAM_SESSION_ID', '').strip():
        raise HTTPException(
            status_code=401,
            detail=(
                'Instagram stories require authentication. '
                'Set INSTAGRAM_SESSION_ID in the environment with a valid sessionid cookie value.'
            ),
        )

    try:
        data, user_id, request_log = _fetch_user_stories(username)
    except Exception as e:
        raise HTTPException(status_code=502, detail=str(e)) from e

    if response is not None:
        _set_metrics_headers(response, request_log)

    if not data or not user_id:
        raise HTTPException(
            status_code=503,
            detail=(
                'Instagram stories API returned no data. '
                'The account may be private, not found, or the session may be expired.'
            ),
        )

    reels = (data.get('reels') or {})
    user_reel = reels.get(user_id) or {}
    user_info = user_reel.get('user') or {}
    items = user_reel.get('items') or []

    if not user_reel:
        raise HTTPException(status_code=404, detail=f'User @{username} not found or has no active stories')

    return {
        'username': username,
        'user_id': user_id,
        'full_name': user_info.get('full_name'),
        'stories': [_story_item_to_story(item) for item in items],
    }
