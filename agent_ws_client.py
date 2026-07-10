"""WebSocket adapter that hands final speech text to the existing Hey-Rice agent."""
from __future__ import annotations

import asyncio
import json
from collections.abc import Callable
from typing import Any

import websockets

from voice_protocol import ProtocolError, parse_agent_transcript


async def process_agent_event(
    raw: str | bytes,
    handler: Callable[[str], dict[str, Any]],
) -> dict[str, Any]:
    event = parse_agent_transcript(raw)
    try:
        result = await asyncio.to_thread(handler, event.text)
        if not isinstance(result, dict):
            raise TypeError("agent handler must return a dictionary")
    except Exception:
        result = {"error": "agent processing failed"}
    return {
        "type": "agent_result",
        "requestId": event.request_id,
        "result": result,
    }


class AgentWsClient:
    def __init__(
        self,
        url: str,
        handler: Callable[[str], dict[str, Any]],
        *,
        connector=websockets.connect,
    ) -> None:
        self.url = url
        self.handler = handler
        self.connector = connector

    async def run_once(self) -> None:
        websocket = await self.connector(self.url, max_size=1024 * 1024)
        try:
            async for raw in websocket:
                if isinstance(raw, bytes):
                    continue
                try:
                    response = await process_agent_event(raw, self.handler)
                except ProtocolError:
                    continue
                await websocket.send(json.dumps(response, ensure_ascii=False))
        finally:
            await websocket.close()

    async def run_forever(self) -> None:
        delay = 0.25
        while True:
            try:
                await self.run_once()
                delay = 0.25
            except asyncio.CancelledError:
                raise
            except Exception:
                await asyncio.sleep(delay)
                delay = min(delay * 2, 5.0)
