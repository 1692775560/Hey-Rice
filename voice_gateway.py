"""Local wake-word gate and WebSocket bridge for browser and Hey-Rice agent.

会话行为:唤醒词只需说一次。唤醒后进入持续对话(CONVERSING)——每说完一句直接
识别、无需再喊唤醒词;直到:
  - agent 判定为 STOP_FEED(用户说"不吃了")→ 结束,回到待唤醒;或
  - 连续 conversation_timeout_ms(默认 10 分钟)无有效交互 → 结束,回到待唤醒。
说话过程中不计入空闲计时。
"""
from __future__ import annotations

import asyncio
import json
import time
import uuid
from enum import Enum
from urllib.parse import urlsplit

import websockets

from audio_utils import UtteranceDetector, normalize_transcript, pcm_rms
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
    CONVERSING = "conversing"   # 已唤醒,等待下一句(无需再唤醒)
    LISTENING = "listening"     # 正在录制一句话
    FINALIZING = "finalizing"


class AgentHub:
    def __init__(self) -> None:
        self.agents: set = set()
        self.pending: dict[str, object] = {}   # requestId -> VoiceSession

    def register(self, websocket) -> None:
        self.agents.add(websocket)

    def unregister(self, websocket) -> None:
        self.agents.discard(websocket)

    async def publish(self, event: dict, session) -> bool:
        payload = json.dumps(event, ensure_ascii=False)
        for agent in tuple(self.agents):
            try:
                await agent.send(payload)
            except Exception:
                self.unregister(agent)
                continue
            self.pending[event["requestId"]] = session
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

        session = self.pending.pop(message.request_id, None)
        if session is None:
            return
        await session.deliver_agent_result(message.request_id, message.result)

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
        self.conversing = False        # 唤醒一次后为 True,直到 STOP_FEED / 超时
        self.last_activity = 0.0       # 上次有效交互(唤醒/说完一句)时间戳(ms)
        self.detector = UtteranceDetector(
            sample_rate=config.sample_rate,
            speech_threshold=config.speech_threshold,
            silence_ms=config.silence_ms,
            no_speech_ms=config.no_speech_ms,
            max_duration_ms=config.max_duration_ms,
        )

    # ---- 空闲计时 ----
    def _now_ms(self) -> float:
        return time.monotonic() * 1000

    def _mark_activity(self) -> None:
        self.last_activity = self._now_ms()

    def _idle_expired(self) -> bool:
        return self._now_ms() - self.last_activity >= self.config.conversation_timeout_ms

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
        elif self.state == SessionState.CONVERSING:
            await self._handle_conversing_frame(message)
        elif self.state == SessionState.LISTENING:
            await self._handle_asr_frame(message)
        # FINALIZING:识别收尾的极短窗口,丢弃期间的音频帧

    async def _start_listening(self) -> None:
        if self.asr is not None:
            await self.asr.close()
            self.asr = None
        self.conversing = False
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
        self.conversing = False
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
            await self._after_utterance()
            return

        self.conversing = True
        self._mark_activity()
        self.detector.reset()
        self.state = SessionState.LISTENING
        await self._send({"type": "wake_detected", "wakeWord": self.config.wake_word})
        await self._send(
            {"type": "status", "state": SessionState.LISTENING.value, "message": "我在听，请说"}
        )

    async def _handle_conversing_frame(self, frame: bytes) -> None:
        # 已唤醒,等待下一句;超过空闲上限则结束会话回到待唤醒。
        if self._idle_expired():
            await self._end_conversation("好一会儿没动静啦，这顿先到这。")
            return
        if pcm_rms(frame) < self.config.speech_threshold:
            return   # 还没开始说话,继续等(不占用 ASR 连接)
        # 检测到说话,开一句新的 ASR。
        try:
            self.asr = self.asr_factory()
            await self.asr.start()
            await self.asr.send_audio(frame)
        except Exception:
            await self._error("asr_failed", "语音识别服务暂时不可用，请稍后重试")
            await self._after_utterance()
            return
        self.detector.reset()
        self.detector.feed(frame)
        self.state = SessionState.LISTENING
        await self._send(
            {"type": "status", "state": SessionState.LISTENING.value, "message": "我在听，请说"}
        )

    async def _handle_asr_frame(self, frame: bytes) -> None:
        try:
            await self.asr.send_audio(frame)
            end_reason = self.detector.feed(frame)
        except Exception:
            await self._error("asr_failed", "语音识别失败，请重新说")
            await self._after_utterance()
            return
        if end_reason is None:
            return

        self.state = SessionState.FINALIZING
        await self._send(
            {"type": "status", "state": self.state.value, "message": "正在识别"}
        )
        try:
            text = normalize_transcript(
                await self.asr.finish(timeout=8), self.config.wake_word
            )
        except Exception:
            await self._error("asr_failed", "语音识别失败，请重新说")
            await self._after_utterance()
            return

        if not text:
            # 没听清:不结束会话,继续等下一句(无需再唤醒)。
            await self._after_utterance(soft_hint="没听清，直接再说一次就行")
            return

        self._mark_activity()
        request_id = str(uuid.uuid4())
        try:
            event = build_agent_transcript(request_id, self.session_id, text)
        except ProtocolError:
            await self._after_utterance()
            return
        await self._send(
            {"type": "final_transcript", "requestId": request_id, "text": text}
        )
        delivered = await self.agent_hub.publish(event, self)
        if not delivered:
            await self._error("agent_unavailable", "Hey-Rice agent 尚未连接")
        await self._after_utterance()

    async def _after_utterance(self, *, soft_hint: str | None = None) -> None:
        """一句话处理完:会话中→回到 CONVERSING 等下一句;否则→待唤醒。"""
        if self.asr is not None:
            await self.asr.close()
            self.asr = None
        self.detector.reset()
        if self.conversing:
            self.state = SessionState.CONVERSING
            message = soft_hint or "还在听，直接说就行（说“不吃了”就结束）"
            await self._send(
                {"type": "status", "state": SessionState.LISTENING.value, "message": message}
            )
        else:
            self.wake_detector.reset()
            self.state = SessionState.WAITING_WAKE
            await self._send(
                {
                    "type": "status",
                    "state": SessionState.WAITING_WAKE.value,
                    "message": f"请说：{self.config.wake_word}",
                }
            )

    async def _end_conversation(self, reason: str) -> None:
        """结束一次对话会话(STOP_FEED 或空闲超时),回到待唤醒。"""
        self.conversing = False
        if self.asr is not None:
            await self.asr.close()
            self.asr = None
        self.detector.reset()
        self.wake_detector.reset()
        self.state = SessionState.WAITING_WAKE
        await self._send(
            {
                "type": "status",
                "state": SessionState.WAITING_WAKE.value,
                "message": f"{reason} 需要时说“{self.config.wake_word}”叫我。",
            }
        )

    async def deliver_agent_result(self, request_id: str, result: dict) -> None:
        """收到 agent 结果:转发给浏览器;若是 STOP_FEED / 一顿饭结束则结束会话。"""
        try:
            await self.browser.send(
                json.dumps(
                    {"type": "agent_result", "requestId": request_id, "result": result},
                    ensure_ascii=False,
                )
            )
        except Exception:
            return
        intent = result.get("intent") if isinstance(result, dict) else None
        action = result.get("action") if isinstance(result, dict) else None
        meal_ended = bool(action.get("meal_ended")) if isinstance(action, dict) else False
        if (intent == "STOP_FEED" or meal_ended) and self.conversing:
            await self._end_conversation("这顿饭结束啦。")

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
