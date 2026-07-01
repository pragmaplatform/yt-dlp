"""Provider-scoped API routers."""

from fastapi import APIRouter, Depends

from api.auth import verify_bearer_token
from api.routes import instagram, tiktok, twitch, twitter, youtube

router = APIRouter(dependencies=[Depends(verify_bearer_token)])
router.include_router(youtube.router, prefix='/youtube', tags=['youtube'])
router.include_router(twitch.router, prefix='/twitch', tags=['twitch'])
router.include_router(tiktok.router, prefix='/tiktok', tags=['tiktok'])
router.include_router(instagram.router, prefix='/instagram', tags=['instagram'])
router.include_router(twitter.router, prefix='/twitter', tags=['twitter'])
