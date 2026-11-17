"""WebSocket endpoints relaying worker events to authenticated dashboard clients."""
from __future__ import annotations

import asyncio

from fastapi import APIRouter, WebSocket, WebSocketDisconnect

from app.events import CHANNELS, bus
from app.security import ws_user

router = APIRouter()


async def _relay(ws: WebSocket, channel: str) -> None:
    if ws_user(ws) is None:
        # Accept first, then close with an application code: closing before accept turns
        # into an HTTP 403 handshake failure, which browsers only surface as code 1006 -
        # the client could not tell "bad token" (log out) from "server down" (retry).
        await ws.accept()
        await ws.close(code=4401, reason="authentication required")
        return
    await ws.accept()
    q = bus.subscribe(channel)
    try:
        await ws.send_json({"type": "hello", "channel": channel})
        while True:
            try:
                msg = await asyncio.wait_for(q.get(), timeout=20)
                await ws.send_text(msg)
            except asyncio.TimeoutError:
                await ws.send_json({"type": "ping"})  # heartbeat so clients can detect dead sockets
    except (WebSocketDisconnect, RuntimeError):
        pass
    finally:
        bus.unsubscribe(channel, q)


def _make(channel: str):
    async def endpoint(ws: WebSocket) -> None:
        await _relay(ws, channel)
    endpoint.__name__ = f"ws_{channel}"
    return endpoint


for _ch in CHANNELS:
    router.add_api_websocket_route(f"/ws/{_ch}", _make(_ch))
