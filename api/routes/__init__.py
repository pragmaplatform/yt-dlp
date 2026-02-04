"""Provider-scoped API routers."""

from fastapi import APIRouter, Depends

from api.auth import verify_bearer_token
from api.routes import twitch, youtube

router = APIRouter(dependencies=[Depends(verify_bearer_token)])
router.include_router(youtube.router, prefix='/youtube', tags=['youtube'])
router.include_router(twitch.router, prefix='/twitch', tags=['twitch'])
