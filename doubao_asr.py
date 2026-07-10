"""One-utterance streaming client for Doubao/Volcengine ASR V3."""
from __future__ import annotations

import asyncio
import contextlib
import uuid
from typing import Any, Callable

import websockets
from volcengine_audio import (
    STTAudioFormatV3,
    VolcengineAsrFunctionsV3,
    VolcengineAsrRequestV3,
)

from voice_config import VoiceConfig


class DoubaoAsrError(RuntimeError):
    pass


class DoubaoAsrSession:
    def __init__(
        self,
        config: VoiceConfig,
        *,
        connector: Callable[..., Any] = websockets.connect,
        protocol: type = VolcengineAsrFunctionsV3,
    ) -> None:
        self.config = config
        self.connector = connector
        self.protocol = protocol
        self.websocket = None
        self.sequence = 0
        self.latest_text = ""
        self.reader_task: asyncio.Task | None = None
        self.final_result: asyncio.Future[str] | None = None

    async def start(self) -> None:
        if not self.config.app_id:
            raise DoubaoAsrError("missing DOUBAO_APP_ID")
        if not self.config.access_token:
            raise DoubaoAsrError("missing DOUBAO_ACCESS_TOKEN")
        if self.websocket is not None:
            raise DoubaoAsrError("ASR session is already started")

        request_id = str(uuid.uuid4())
        headers = {
            "X-Api-App-Key": self.config.app_id,
            "X-Api-Access-Key": self.config.access_token,
            "X-Api-Resource-Id": self.config.resource_id,
            "X-Api-Request-Id": request_id,
            "X-Api-Sequence": "-1",
        }
        try:
            self.websocket = await self.connector(
                self.config.asr_endpoint,
                additional_headers=headers,
                max_size=None,
                open_timeout=10,
            )
        except Exception as exc:
            raise DoubaoAsrError("unable to connect to Doubao ASR") from exc

        request = VolcengineAsrRequestV3(
            user=VolcengineAsrRequestV3.User(uid="hey-rice-voice-gateway"),
            audio=VolcengineAsrRequestV3.Audio(
                format=STTAudioFormatV3.pcm,
                rate=16000,
                bits=16,
                channel=1,
            ),
            request=VolcengineAsrRequestV3.Request(
                model_name="bigmodel",
                enable_itn=True,
                enable_punc=True,
                enable_ddc=True,
                enable_nonstream=True,
                show_utterances=True,
                end_window_size=max(200, self.config.silence_ms),
            ),
        ).model_dump(mode="json", exclude_none=True)

        self.sequence = 1
        packet = self.protocol.generate_asr_full_client_request(
            sequence=self.sequence,
            request_params=request,
            compression=True,
        )
        await self.websocket.send(packet)
        self.final_result = asyncio.get_running_loop().create_future()
        self.reader_task = asyncio.create_task(self._read_responses())

    async def send_audio(self, pcm: bytes) -> None:
        if self.websocket is None:
            raise DoubaoAsrError("ASR session has not started")
        if not pcm:
            return
        self.sequence += 1
        packet = self.protocol.generate_asr_audio_only_request(
            sequence=self.sequence,
            audio=pcm,
            compress=True,
        )
        await self.websocket.send(packet)

    async def finish(self, *, timeout: float = 8.0) -> str:
        if self.websocket is None or self.final_result is None:
            raise DoubaoAsrError("ASR session has not started")
        self.sequence += 1
        packet = self.protocol.generate_asr_audio_only_request(
            sequence=self.sequence,
            audio=b"",
            compress=False,
        )
        await self.websocket.send(packet)
        try:
            return await asyncio.wait_for(asyncio.shield(self.final_result), timeout)
        except TimeoutError as exc:
            raise DoubaoAsrError("Doubao ASR final response timed out") from exc

    async def close(self) -> None:
        if self.reader_task is not None and not self.reader_task.done():
            self.reader_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self.reader_task
        self.reader_task = None
        if self.websocket is not None:
            with contextlib.suppress(Exception):
                await self.websocket.close()
        self.websocket = None

    async def _read_responses(self) -> None:
        try:
            while True:
                response = await self.websocket.recv()
                parsed = self.protocol.parse_response(response)
                code = parsed.get("code")
                if code:
                    raise DoubaoAsrError(f"Doubao ASR error code {code}")

                message = parsed.get("message")
                if isinstance(message, dict):
                    result = message.get("result")
                    if isinstance(result, dict):
                        text = result.get("text")
                        if isinstance(text, str) and text.strip():
                            self.latest_text = text.strip()

                if parsed.get("is_last_package"):
                    if self.final_result is not None and not self.final_result.done():
                        self.final_result.set_result(self.latest_text)
                    return
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            if self.final_result is not None and not self.final_result.done():
                error = exc if isinstance(exc, DoubaoAsrError) else DoubaoAsrError(
                    "Doubao ASR connection closed before final result"
                )
                self.final_result.set_exception(error)
