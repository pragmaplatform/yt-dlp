"""Bearer token auth using YT_DLP_API_SECRET env variable."""

import os

from fastapi import Header, HTTPException


def verify_bearer_token(authorization: str | None = Header(None, alias='Authorization')) -> None:
    """Require Authorization: Bearer <YT_DLP_API_SECRET> on every request. Raises 401/503 if missing or invalid."""
    secret = os.environ.get('YT_DLP_API_SECRET')
    if not secret:
        raise HTTPException(status_code=503, detail='YT_DLP_API_SECRET not configured')
    if not authorization or not authorization.startswith('Bearer '):
        raise HTTPException(status_code=401, detail='Missing or invalid Authorization header')
    token = authorization[7:].strip()
    if token != secret:
        raise HTTPException(status_code=401, detail='Invalid token')
