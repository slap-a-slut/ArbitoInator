"""Optional bridge from Python -> UI websocket server.

The UI lives in ./ui (node server). It exposes:
  - GET  /          serves index.html
  - POST /push      broadcasts JSON to all websocket clients

Python side posts to http://localhost:8080/push.
If the UI server isn't running, we silently ignore errors.
"""

from __future__ import annotations

import aiohttp


async def ui_push(payload: dict, url: str = "http://localhost:8080/push", timeout: int = 1) -> None:
    try:
        async with aiohttp.ClientSession() as session:
            async with session.post(url, json=payload, timeout=timeout):
                return
    except Exception:
        # UI is optional: do nothing if it's not running.
        return
