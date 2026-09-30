"""Entry point.

    uvicorn app.main:app --host 0.0.0.0 --port 8000        (API + bot in one process)
    python -m app.main                                     (same, via uvicorn)

Starting refuses with a clear message if the configuration is not allowed (for example live mode).
"""

import logging
import os

from app.api.app import create_app

logging.basicConfig(level=os.environ.get("LOG_LEVEL", "INFO"),
                    format="%(asctime)s %(levelname)s %(name)s: %(message)s")

app = create_app()

if __name__ == "__main__":
    import uvicorn

    uvicorn.run("app.main:app", host=os.environ.get("HOST", "127.0.0.1"), port=int(os.environ.get("PORT", "8000")))
