"""Optional bridge from Python -> UI websocket server.

The UI lives in ./ui (node server). It exposes:
  - GET  /          serves index.html
  - POST /push      broadcasts JSON to all websocket clients

Python side posts to http://localhost:8080/push.
If the UI server isn't running, we silently ignore errors.
"""

from __future__ import annotations

import os
from typing import Optional

import aiohttp


def _ui_url() -> str:
    host = os.getenv("UI_HOST", "localhost").strip() or "localhost"
    port = os.getenv("UI_PORT", "8080").strip() or "8080"
    return f"http://{host}:{port}/push"


async def ui_push(payload: dict, url: Optional[str] = None, timeout: int = 1) -> None:
    if url is None:
        url = _ui_url()
    try:
        async with aiohttp.ClientSession() as session:
            async with session.post(url, json=payload, timeout=timeout):
                return
    except Exception:
        # UI is optional: do nothing if it's not running.
        return
