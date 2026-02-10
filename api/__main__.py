"""Run the Metadata API server (python -m api or yt-dlp-api)."""

import os

import uvicorn

from api.app import app


def main() -> None:
    host = os.environ.get('YT_DLP_API_HOST', '127.0.0.1')
    port = int(os.environ.get('PORT') or os.environ.get('YT_DLP_API_PORT', '8000'))
    uvicorn.run(app, host=host, port=port)


if __name__ == '__main__':
    main()
