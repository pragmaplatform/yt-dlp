"""FastAPI app: mounts provider-scoped routers."""

import os

from fastapi import FastAPI

from api.routes import router as api_router


def _load_env() -> None:
    try:
        from dotenv import load_dotenv
        load_dotenv()
    except ImportError:
        pass


app = FastAPI(
    title='yt-dlp Metadata API',
    description='HTTP API for video metadata (no download). Extensible to more providers and data types.',
    on_startup=[_load_env],
)


@app.get('/health')
def health() -> dict[str, str]:
    """Unauthenticated health check for Render and load balancers."""
    return {'status': 'ok'}


app.include_router(api_router)
