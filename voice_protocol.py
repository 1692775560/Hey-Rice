"""JSON contracts shared by browser, speech gateway, and agent clients."""
from __future__ import annotations

import json
import time
from dataclasses import dataclass
from typing import Any


class ProtocolError(ValueError):
    """Raised when a WebSocket text message violates the public contract."""


@dataclass(frozen=True)
class BrowserControl:
    type: str
    sample_rate: int | None = None


@dataclass(frozen=True)
class AgentResult:
    request_id: str
    result: dict[str, Any]


@dataclass(frozen=True)
class AgentTranscript:
    request_id: str
    session_id: str
    text: str
    timestamp_ms: int


def _object_json(raw: str | bytes) -> dict[str, Any]:
    try:
        value = json.loads(raw)
    except (json.JSONDecodeError, UnicodeDecodeError, TypeError) as exc:
        raise ProtocolError("message must be valid JSON") from exc
    if not isinstance(value, dict):
        raise ProtocolError("message must be a JSON object")
    return value


def parse_browser_control(raw: str | bytes) -> BrowserControl:
    value = _object_json(raw)
    message_type = value.get("type")
    if message_type == "stop":
        return BrowserControl(type="stop")
    if message_type != "start":
        raise ProtocolError(f"unsupported browser message type: {message_type!r}")

    sample_rate = value.get("sampleRate")
    if sample_rate != 16000:
        raise ProtocolError("browser audio sampleRate must be 16000")
    return BrowserControl(type="start", sample_rate=sample_rate)


def build_agent_transcript(
    request_id: str,
    session_id: str,
    text: str,
    timestamp_ms: int | None = None,
) -> dict[str, Any]:
    normalized = text.strip()
    if not normalized:
        raise ProtocolError("final transcript text must not be blank")
    if not request_id or not session_id:
        raise ProtocolError("requestId and sessionId are required")
    return {
        "type": "final_transcript",
        "requestId": request_id,
        "sessionId": session_id,
        "text": normalized,
        "timestamp": timestamp_ms if timestamp_ms is not None else int(time.time() * 1000),
    }


def parse_agent_result(raw: str | bytes) -> AgentResult:
    value = _object_json(raw)
    if value.get("type") != "agent_result":
        raise ProtocolError("unsupported agent message type")
    request_id = value.get("requestId")
    result = value.get("result")
    if not isinstance(request_id, str) or not request_id:
        raise ProtocolError("agent_result requestId is required")
    if not isinstance(result, dict):
        raise ProtocolError("agent_result result must be an object")
    return AgentResult(request_id=request_id, result=result)


def parse_agent_transcript(raw: str | bytes) -> AgentTranscript:
    value = _object_json(raw)
    if value.get("type") != "final_transcript":
        raise ProtocolError("unsupported agent event type")
    request_id = value.get("requestId")
    session_id = value.get("sessionId")
    text = value.get("text")
    timestamp = value.get("timestamp")
    if not isinstance(request_id, str) or not request_id:
        raise ProtocolError("final_transcript requestId is required")
    if not isinstance(session_id, str) or not session_id:
        raise ProtocolError("final_transcript sessionId is required")
    if not isinstance(text, str) or not text.strip():
        raise ProtocolError("final_transcript text is required")
    if not isinstance(timestamp, int):
        raise ProtocolError("final_transcript timestamp must be an integer")
    return AgentTranscript(request_id, session_id, text.strip(), timestamp)
