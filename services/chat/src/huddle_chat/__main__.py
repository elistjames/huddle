"""Entrypoint: `python -m huddle_chat` serves the chat service on port 8001."""

from __future__ import annotations

import uvicorn

from huddle_chat.app import create_app
from huddle_chat.config import Settings
from huddle_chat.obs import configure_logging


def main() -> None:
    configure_logging()
    settings = Settings.from_env()
    uvicorn.run(
        create_app(settings),
        host=settings.host,
        port=settings.port,
        log_config=None,  # keep our JSON logging
        access_log=False,  # TraceMiddleware logs requests with the §9 fields
    )


if __name__ == "__main__":
    main()
