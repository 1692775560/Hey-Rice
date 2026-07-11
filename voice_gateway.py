"""Push-to-talk WebSocket bridge for browser and Hey-Rice agent.

按键对讲(PTT):浏览器按下按钮发 speak_start 开始录音,松开发 speak_end 定稿。
无需唤醒词、无需本地唤醒模型;每次按压即一句话:
  READY --speak_start--> LISTENING(流式送豆包 ASR) --speak_end--> 定稿 --> READY
最终文本经 /ws/agent 交给现有 agent(server.process),结果回传浏览器。
"""
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


class SessionState(str, Enum):
    IDLE = "idle"          # 麦克风未开启
    READY = "ready"        # 麦克风就绪,等待按下说话
    LISTENING = "listening"  # 按住中,正在录一句话
    FINALIZING = "finalizing"


class AgentHub:
    def __init__(self) -> None:
        self.agents: set = set()
        self.pending: dict[str, object] = {}   # requestId -> browser

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
        asr_factory,
        agent_hub: AgentHub,
    ) -> None:
        self.browser = browser
        self.config = config
        self.asr_factory = asr_factory
        self.agent_hub = agent_hub
        self.session_id = str(uuid.uuid4())
        self.state = SessionState.IDLE
        self.asr = None
        # 仅用于"按住过久"的安全上限,正常结束由松开(speak_end)触发。
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
            await self._close(notify=False)

    async def handle(self, message: str | bytes) -> None:
        if isinstance(message, str):
            try:
                control = parse_browser_control(message)
            except ProtocolError as exc:
                await self._error("invalid_control", str(exc))
                return
            if control.type == "start":
                await self._open()
            elif control.type == "speak_start":
                await self._begin_utterance()
            elif control.type == "speak_end":
                await self._finish_utterance()
            else:  # stop
                await self._close()
            return

        # 二进制音频帧:仅在按住录音(LISTENING)时才送 ASR,其余忽略。
        if self.state != SessionState.LISTENING:
            return
        if not message:
            return
        if len(message) % 2:
            await self._error("invalid_audio", "音频帧不是有效的 PCM16 数据")
            return
        await self._handle_audio_frame(message)

    async def _open(self) -> None:
        if self.asr is not None:
            await self.asr.close()
            self.asr = None
        self.detector.reset()
        self.state = SessionState.READY
        await self._send(
            {"type": "status", "state": self.state.value, "message": "麦克风就绪，按住说话"}
        )

    async def _close(self, *, notify: bool = True) -> None:
        if self.asr is not None:
            await self.asr.close()
            self.asr = None
        self.detector.reset()
        self.state = SessionState.IDLE
        if notify:
            await self._send(
                {"type": "status", "state": self.state.value, "message": "麦克风已关闭"}
            )

    async def _begin_utterance(self) -> None:
        # 只在就绪态开始录音;重复的 speak_start 忽略。
        if self.state not in (SessionState.READY, SessionState.FINALIZING):
            return
        try:
            self.asr = self.asr_factory()
            await self.asr.start()
        except Exception:
            await self._error("asr_failed", "语音识别服务暂时不可用，请稍后重试")
            await self._to_ready()
            return
        self.detector.reset()
        self.state = SessionState.LISTENING
        await self._send(
            {"type": "status", "state": self.state.value, "message": "正在录音，请说…"}
        )

    async def _handle_audio_frame(self, frame: bytes) -> None:
        try:
            await self.asr.send_audio(frame)
            end_reason = self.detector.feed(frame)
        except Exception:
            await self._error("asr_failed", "语音识别失败，请重试")
            await self._to_ready()
            return
        # 松开会触发 speak_end;这里只兜底"按住过久"的最大时长。
        if end_reason is not None and end_reason.value == "max_duration":
            await self._finish_utterance()

    async def _finish_utterance(self) -> None:
        if self.state != SessionState.LISTENING:
            return
        self.state = SessionState.FINALIZING
        await self._send(
            {"type": "status", "state": self.state.value, "message": "正在识别…"}
        )
        try:
            text = normalize_transcript(
                await self.asr.finish(timeout=8), self.config.wake_word
            )
        except Exception:
            await self._error("asr_failed", "语音识别失败，请重试")
            await self._to_ready()
            return

        if not text:
            await self._to_ready(hint="没听清，按住再说一次")
            return

        request_id = str(uuid.uuid4())
        try:
            event = build_agent_transcript(request_id, self.session_id, text)
        except ProtocolError:
            await self._to_ready()
            return
        await self._send(
            {"type": "final_transcript", "requestId": request_id, "text": text}
        )
        delivered = await self.agent_hub.publish(event, self.browser)
        if not delivered:
            await self._error("agent_unavailable", "Hey-Rice agent 尚未连接")
        await self._to_ready()

    async def _to_ready(self, *, hint: str | None = None) -> None:
        if self.asr is not None:
            await self.asr.close()
            self.asr = None
        self.detector.reset()
        self.state = SessionState.READY
        await self._send(
            {
                "type": "status",
                "state": self.state.value,
                "message": hint or "麦克风就绪，按住说话",
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
        asr_factory=None,
    ) -> None:
        self.config = config
        self.agent_hub = AgentHub()
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

        session = VoiceSession(
            websocket,
            self.config,
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
