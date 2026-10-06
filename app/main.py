"""Entry point.

    uvicorn app.main:app --host 0.0.0.0 --port 8000        (API + bot in one process)
    python -m app.main                                     (same, via uvicorn)

Starting refuses with a clear message if the configuration is not allowed (for example live mode).
Logs go to logs/bot.log and logs/http.log; the console shows only warnings and milestones (app/core/logging_setup.py).
"""

import os

from app.api.app import create_app
from app.core.logging_setup import configure_logging

configure_logging()

app = create_app()

if __name__ == "__main__":
    import uvicorn

    uvicorn.run("app.main:app", host=os.environ.get("HOST", "127.0.0.1"), port=int(os.environ.get("PORT", "8000")))
