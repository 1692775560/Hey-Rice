"""Local wake-word gate and WebSocket bridge for browser and Hey-Rice agent."""
from __future__ import annotations

import asyncio
import json
import uuid
from enum import Enum
from urllib.parse import urlsplit

import websockets

from audio_utils import UtteranceDetector, normalize_transcript
from doubao_asr import DoubaoAsrSession
from voice_config import VoiceConfig
from voice_protocol import (
    ProtocolError,
    build_agent_transcript,
    parse_agent_result,
    parse_browser_control,
)
from wakeword import WakeWordDetector


class SessionState(str, Enum):
    IDLE = "idle"
    WAITING_WAKE = "waiting_wake"
    LISTENING = "listening"
    FINALIZING = "finalizing"


class AgentHub:
    def __init__(self) -> None:
        self.agents: set = set()
        self.pending: dict[str, object] = {}

    def register(self, websocket) -> None:
        self.agents.add(websocket)

    def unregister(self, websocket) -> None:
        self.agents.discard(websocket)

    async def publish(self, event: dict, browser) -> bool:
        payload = json.dumps(event, ensure_ascii=False)
        for agent in tuple(self.agents):
            try:
                await agent.send(payload)
            except Exception:
                self.unregister(agent)
                continue
            self.pending[event["requestId"]] = browser
            return True
        return False

    async def handle_message(self, agent, raw: str | bytes) -> None:
        try:
            message = parse_agent_result(raw)
        except ProtocolError as exc:
            await agent.send(
                json.dumps(
                    {"type": "error", "code": "invalid_agent_message", "message": str(exc)},
                    ensure_ascii=False,
                )
            )
            return

        browser = self.pending.pop(message.request_id, None)
        if browser is None:
            return
        try:
            await browser.send(
                json.dumps(
                    {
                        "type": "agent_result",
                        "requestId": message.request_id,
                        "result": message.result,
                    },
                    ensure_ascii=False,
                )
            )
        except Exception:
            return

    async def serve(self, websocket) -> None:
        self.register(websocket)
        try:
            async for message in websocket:
                if isinstance(message, bytes):
                    await websocket.send(
                        json.dumps(
                            {
                                "type": "error",
                                "code": "invalid_agent_message",
                                "message": "agent messages must be JSON text",
                            },
                            ensure_ascii=False,
                        )
                    )
                    continue
                await self.handle_message(websocket, message)
        finally:
            self.unregister(websocket)


class VoiceSession:
    def __init__(
        self,
        browser,
        config: VoiceConfig,
        *,
        wake_detector,
        asr_factory,
        agent_hub: AgentHub,
    ) -> None:
        self.browser = browser
        self.config = config
        self.wake_detector = wake_detector
        self.asr_factory = asr_factory
        self.agent_hub = agent_hub
        self.session_id = str(uuid.uuid4())
        self.state = SessionState.IDLE
        self.asr = None
        self.detector = UtteranceDetector(
            sample_rate=config.sample_rate,
            speech_threshold=config.speech_threshold,
            silence_ms=config.silence_ms,
            no_speech_ms=config.no_speech_ms,
            max_duration_ms=config.max_duration_ms,
        )

    async def run(self) -> None:
        await self._send(
            {
                "type": "status",
                "state": SessionState.IDLE.value,
                "message": "语音服务已连接，等待开启麦克风",
            }
        )
        try:
            async for message in self.browser:
                await self.handle(message)
        finally:
            await self._stop(notify=False)

    async def handle(self, message: str | bytes) -> None:
        if isinstance(message, str):
            try:
                control = parse_browser_control(message)
            except ProtocolError as exc:
                await self._error("invalid_control", str(exc))
                return
            if control.type == "start":
                await self._start_listening()
            else:
                await self._stop()
            return

        if self.state == SessionState.IDLE:
            await self._error("microphone_not_started", "请先开启麦克风")
            return
        if not message:
            return
        if len(message) % 2:
            await self._error("invalid_audio", "音频帧不是有效的 PCM16 数据")
            return

        if self.state == SessionState.WAITING_WAKE:
            await self._handle_wake_frame(message)
        elif self.state == SessionState.LISTENING:
            await self._handle_asr_frame(message)

    async def _start_listening(self) -> None:
        if self.asr is not None:
            await self.asr.close()
            self.asr = None
        self.detector.reset()
        self.wake_detector.reset()
        self.state = SessionState.WAITING_WAKE
        await self._send(
            {
                "type": "status",
                "state": self.state.value,
                "message": f"请说：{self.config.wake_word}",
            }
        )

    async def _stop(self, *, notify: bool = True) -> None:
        if self.asr is not None:
            await self.asr.close()
            self.asr = None
        self.detector.reset()
        self.state = SessionState.IDLE
        if notify:
            await self._send(
                {
                    "type": "status",
                    "state": self.state.value,
                    "message": "麦克风已关闭",
                }
            )

    async def _handle_wake_frame(self, frame: bytes) -> None:
        try:
            hit = await asyncio.to_thread(self.wake_detector.accept, frame)
            if not hit:
                return
            self.asr = self.asr_factory()
            await self.asr.start()
        except Exception:
            await self._error("asr_failed", "语音识别服务暂时不可用，请稍后重试")
            await self._reset_after_utterance()
            return

        self.detector.reset()
        self.state = SessionState.LISTENING
        await self._send({"type": "wake_detected", "wakeWord": self.config.wake_word})
        await self._send(
            {"type": "status", "state": self.state.value, "message": "我在听，请说"}
        )

    async def _handle_asr_frame(self, frame: bytes) -> None:
        try:
            await self.asr.send_audio(frame)
            end_reason = self.detector.feed(frame)
        except Exception:
            await self._error("asr_failed", "语音识别失败，请重新说唤醒词")
            await self._reset_after_utterance()
            return
        if end_reason is None:
            return

        try:
            self.state = SessionState.FINALIZING
            await self._send(
                {"type": "status", "state": self.state.value, "message": "正在识别"}
            )
            text = normalize_transcript(
                await self.asr.finish(timeout=8), self.config.wake_word
            )
            if not text:
                await self._error("no_speech", "没有听清，请重新说唤醒词")
                return

            request_id = str(uuid.uuid4())
            event = build_agent_transcript(request_id, self.session_id, text)
            await self._send(
                {"type": "final_transcript", "requestId": request_id, "text": text}
            )
            delivered = await self.agent_hub.publish(event, self.browser)
            if not delivered:
                await self._error("agent_unavailable", "Hey-Rice agent 尚未连接")
        except Exception:
            await self._error("asr_failed", "语音识别失败，请重新说唤醒词")
        finally:
            await self._reset_after_utterance()

    async def _reset_after_utterance(self) -> None:
        if self.asr is not None:
            await self.asr.close()
            self.asr = None
        self.detector.reset()
        self.wake_detector.reset()
        self.state = SessionState.WAITING_WAKE
        await self._send(
            {
                "type": "status",
                "state": self.state.value,
                "message": f"请说：{self.config.wake_word}",
            }
        )

    async def _error(self, code: str, message: str) -> None:
        await self._send({"type": "error", "code": code, "message": message})

    async def _send(self, payload: dict) -> None:
        await self.browser.send(json.dumps(payload, ensure_ascii=False))


class VoiceGateway:
    def __init__(
        self,
        config: VoiceConfig,
        *,
        wake_factory=None,
        asr_factory=None,
    ) -> None:
        self.config = config
        self.agent_hub = AgentHub()
        self.wake_factory = wake_factory or (
            lambda: WakeWordDetector(config.kws_model_dir, sample_rate=config.sample_rate)
        )
        self.asr_factory = asr_factory or (lambda: DoubaoAsrSession(config))

    async def handler(self, websocket) -> None:
        path = urlsplit(websocket.request.path).path
        if path == "/ws/agent":
            await self.agent_hub.serve(websocket)
            return
        if path != "/ws/voice":
            await websocket.close(code=1008, reason="unknown websocket path")
            return

        missing = self.config.missing_requirements()
        if missing:
            await websocket.send(
                json.dumps(
                    {
                        "type": "error",
                        "code": "voice_not_configured",
                        "message": "语音服务缺少配置：" + ", ".join(missing),
                    },
                    ensure_ascii=False,
                )
            )
            return
        try:
            wake_detector = self.wake_factory()
        except Exception:
            await websocket.send(
                json.dumps(
                    {
                        "type": "error",
                        "code": "wakeword_not_ready",
                        "message": "本地唤醒模型未准备好",
                    },
                    ensure_ascii=False,
                )
            )
            return

        session = VoiceSession(
            websocket,
            self.config,
            wake_detector=wake_detector,
            asr_factory=self.asr_factory,
            agent_hub=self.agent_hub,
        )
        await session.run()

    async def serve_forever(self) -> None:
        async with websockets.serve(
            self.handler,
            self.config.websocket_host,
            self.config.websocket_port,
            max_size=1024 * 1024,
        ):
            await asyncio.Future()
