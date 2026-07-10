import asyncio
import unittest

from doubao_asr import DoubaoAsrError, DoubaoAsrSession
from voice_config import VoiceConfig


class FakeProtocol:
    full_requests = []
    audio_requests = []

    @classmethod
    def reset(cls):
        cls.full_requests = []
        cls.audio_requests = []

    @classmethod
    def generate_asr_full_client_request(cls, sequence, request_params, compression):
        cls.full_requests.append((sequence, request_params, compression))
        return b"full-request"

    @classmethod
    def generate_asr_audio_only_request(cls, sequence, audio, compress=True):
        cls.audio_requests.append((sequence, audio, compress))
        return b"audio-request"

    @staticmethod
    def parse_response(response):
        return response


class FakeWebSocket:
    def __init__(self, responses):
        self.responses = asyncio.Queue()
        for response in responses:
            self.responses.put_nowait(response)
        self.sent = []
        self.closed = False

    async def send(self, data):
        self.sent.append(data)

    async def recv(self):
        return await self.responses.get()

    async def close(self):
        self.closed = True


def test_config():
    return VoiceConfig(
        voice_enabled=True,
        websocket_host="127.0.0.1",
        websocket_port=8765,
        wake_word="小瓜小瓜",
        sample_rate=16000,
        kws_model_dir=None,
        app_id="app-id",
        access_token="access-token",
        secret_key="unused-secret",
        resource_id="volc.bigasr.sauc.duration",
        asr_endpoint="wss://example.test/asr",
        silence_ms=900,
        no_speech_ms=4000,
        max_duration_ms=12000,
        speech_threshold=500,
        embedded_agent=True,
    )


class DoubaoAsrTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        FakeProtocol.reset()
        self.connect_calls = []

    def connector_for(self, websocket):
        async def connect(uri, **kwargs):
            self.connect_calls.append((uri, kwargs))
            return websocket

        return connect

    async def test_start_sends_secure_headers_and_pcm_request(self):
        websocket = FakeWebSocket([])
        session = DoubaoAsrSession(
            test_config(),
            connector=self.connector_for(websocket),
            protocol=FakeProtocol,
        )

        await session.start()
        await session.close()

        uri, kwargs = self.connect_calls[0]
        headers = kwargs["additional_headers"]
        self.assertEqual(uri, "wss://example.test/asr")
        self.assertEqual(headers["X-Api-App-Key"], "app-id")
        self.assertEqual(headers["X-Api-Access-Key"], "access-token")
        self.assertNotIn("unused-secret", repr(headers))
        sequence, request, compressed = FakeProtocol.full_requests[0]
        self.assertEqual(sequence, 1)
        self.assertTrue(compressed)
        self.assertEqual(request["audio"]["format"], "pcm")
        self.assertEqual(request["audio"]["rate"], 16000)
        self.assertTrue(request["request"]["enable_nonstream"])
        self.assertTrue(request["request"]["show_utterances"])

    async def test_finish_returns_only_latest_final_text(self):
        websocket = FakeWebSocket(
            [
                {
                    "is_last_package": False,
                    "message": {"result": {"text": "我想"}},
                },
                {
                    "is_last_package": True,
                    "message": {"result": {"text": "我想吃饭"}},
                },
            ]
        )
        session = DoubaoAsrSession(
            test_config(),
            connector=self.connector_for(websocket),
            protocol=FakeProtocol,
        )
        await session.start()
        await session.send_audio(b"\x00\x00" * 1600)

        text = await session.finish(timeout=1)
        await session.close()

        self.assertEqual(text, "我想吃饭")
        self.assertEqual(FakeProtocol.audio_requests[0][0], 2)
        self.assertNotEqual(FakeProtocol.audio_requests[0][1], b"")
        self.assertEqual(FakeProtocol.audio_requests[-1], (3, b"", False))

    async def test_api_error_becomes_safe_exception(self):
        websocket = FakeWebSocket(
            [{"code": 45000000, "message": {"error": "permission denied"}}]
        )
        session = DoubaoAsrSession(
            test_config(),
            connector=self.connector_for(websocket),
            protocol=FakeProtocol,
        )
        await session.start()

        with self.assertRaisesRegex(DoubaoAsrError, "45000000"):
            await session.finish(timeout=1)
        await session.close()

    async def test_missing_credentials_fail_before_connect(self):
        config = test_config()
        object.__setattr__(config, "access_token", "")
        session = DoubaoAsrSession(
            config,
            connector=self.connector_for(FakeWebSocket([])),
            protocol=FakeProtocol,
        )

        with self.assertRaisesRegex(DoubaoAsrError, "DOUBAO_ACCESS_TOKEN"):
            await session.start()
        self.assertEqual(self.connect_calls, [])


if __name__ == "__main__":
    unittest.main()
