import array
import json
import unittest
from dataclasses import replace
from pathlib import Path

from voice_config import VoiceConfig
from voice_gateway import AgentHub, SessionState, VoiceSession


def pcm(amplitude, milliseconds=100):
    return array.array("h", [amplitude] * (16 * milliseconds)).tobytes()


def test_config():
    return VoiceConfig(
        voice_enabled=True,
        websocket_host="127.0.0.1",
        websocket_port=8765,
        wake_word="小瓜小瓜",
        sample_rate=16000,
        kws_model_dir=Path("models/test"),
        app_id="app-id",
        access_token="access-token",
        secret_key="",
        resource_id="volc.bigasr.sauc.duration",
        asr_endpoint="wss://example.test/asr",
        silence_ms=900,
        no_speech_ms=4000,
        max_duration_ms=1000,
        speech_threshold=500,
        embedded_agent=True,
    )


class FakeSocket:
    def __init__(self):
        self.sent = []

    async def send(self, message):
        self.sent.append(message)


class DisconnectingSocket(FakeSocket):
    def __init__(self):
        super().__init__()
        self.closed = False

    def __aiter__(self):
        return self

    async def __anext__(self):
        self.closed = True
        raise StopAsyncIteration

    async def send(self, message):
        if self.closed:
            raise RuntimeError("socket is closed")
        await super().send(message)


class ClosedSocket(FakeSocket):
    async def send(self, message):
        raise RuntimeError("socket is closed")


class FakeAsr:
    def __init__(self, final_text="我想吃饭", start_error=None):
        self.final_text = final_text
        self.start_error = start_error
        self.started = False
        self.closed = False
        self.frames = []
        self.finish_count = 0

    async def start(self):
        if self.start_error:
            raise self.start_error
        self.started = True

    async def send_audio(self, frame):
        self.frames.append(frame)

    async def finish(self, timeout=8):
        self.finish_count += 1
        return self.final_text

    async def close(self):
        self.closed = True


class FakeAgentHub:
    def __init__(self):
        self.events = []

    async def publish(self, event, browser):
        self.events.append((event, browser))
        return True


def decoded_messages(socket):
    return [json.loads(message) for message in socket.sent]


class VoiceSessionTests(unittest.IsolatedAsyncioTestCase):
    def make_session(self, asr):
        browser = FakeSocket()
        hub = FakeAgentHub()
        session = VoiceSession(
            browser,
            test_config(),
            asr_factory=lambda: asr,
            agent_hub=hub,
        )
        return session, browser, hub

    async def test_start_puts_session_ready(self):
        session, _, _ = self.make_session(FakeAsr())
        await session.handle('{"type":"start","sampleRate":16000}')
        self.assertEqual(session.state, SessionState.READY)

    async def test_audio_before_press_is_ignored(self):
        asr = FakeAsr()
        session, _, hub = self.make_session(asr)
        await session.handle('{"type":"start","sampleRate":16000}')

        await session.handle(pcm(2000))

        self.assertFalse(asr.started)
        self.assertEqual(hub.events, [])
        self.assertEqual(session.state, SessionState.READY)

    async def test_speak_start_begins_recording(self):
        asr = FakeAsr()
        session, _, _ = self.make_session(asr)
        await session.handle('{"type":"start","sampleRate":16000}')

        await session.handle('{"type":"speak_start"}')

        self.assertTrue(asr.started)
        self.assertEqual(session.state, SessionState.LISTENING)

    async def test_press_talk_release_publishes_one_transcript(self):
        asr = FakeAsr("小瓜小瓜，我想吃饭")
        session, browser, hub = self.make_session(asr)
        await session.handle('{"type":"start","sampleRate":16000}')
        await session.handle('{"type":"speak_start"}')
        await session.handle(pcm(2000))
        await session.handle(pcm(2000))

        await session.handle('{"type":"speak_end"}')

        self.assertEqual(len(hub.events), 1, decoded_messages(browser))
        event = hub.events[0][0]
        self.assertEqual(event["type"], "final_transcript")
        self.assertEqual(event["text"], "我想吃饭")   # 唤醒词若被识别到会被剥掉
        self.assertEqual(asr.finish_count, 1)
        self.assertTrue(asr.closed)
        self.assertEqual(len(asr.frames), 2)
        self.assertEqual(session.state, SessionState.READY)

    async def test_speak_start_asr_error_returns_ready(self):
        asr = FakeAsr(start_error=RuntimeError("network detail"))
        session, browser, hub = self.make_session(asr)
        await session.handle('{"type":"start","sampleRate":16000}')

        await session.handle('{"type":"speak_start"}')

        self.assertEqual(session.state, SessionState.READY)
        self.assertEqual(hub.events, [])
        errors = [m for m in decoded_messages(browser) if m["type"] == "error"]
        self.assertEqual(errors[0]["code"], "asr_failed")
        self.assertNotIn("network detail", errors[0]["message"])

    async def test_empty_transcript_returns_ready_without_publish(self):
        asr = FakeAsr("")
        session, browser, hub = self.make_session(asr)
        await session.handle('{"type":"start","sampleRate":16000}')
        await session.handle('{"type":"speak_start"}')
        await session.handle(pcm(2000))

        await session.handle('{"type":"speak_end"}')

        self.assertEqual(hub.events, [])
        self.assertEqual(session.state, SessionState.READY)

    async def test_stop_closes_active_asr(self):
        asr = FakeAsr()
        session, _, _ = self.make_session(asr)
        await session.handle('{"type":"start","sampleRate":16000}')
        await session.handle('{"type":"speak_start"}')

        await session.handle('{"type":"stop"}')

        self.assertTrue(asr.closed)
        self.assertEqual(session.state, SessionState.IDLE)

    async def test_second_press_records_again_without_extra_steps(self):
        asrs = []

        def make_asr():
            a = FakeAsr("再来一口")
            asrs.append(a)
            return a

        browser = FakeSocket()
        hub = FakeAgentHub()
        session = VoiceSession(browser, test_config(), asr_factory=make_asr, agent_hub=hub)
        await session.handle('{"type":"start","sampleRate":16000}')

        for _ in range(2):
            await session.handle('{"type":"speak_start"}')
            await session.handle(pcm(2000))
            await session.handle('{"type":"speak_end"}')

        self.assertEqual(len(hub.events), 2)
        self.assertEqual(len(asrs), 2)
        self.assertEqual(session.state, SessionState.READY)

    async def test_disconnect_cleans_up_without_writing_to_closed_socket(self):
        browser = DisconnectingSocket()
        session = VoiceSession(
            browser,
            test_config(),
            asr_factory=lambda: FakeAsr(),
            agent_hub=FakeAgentHub(),
        )

        await session.run()

        self.assertEqual(session.state, SessionState.IDLE)


class AgentHubTests(unittest.IsolatedAsyncioTestCase):
    async def test_result_routes_to_originating_browser(self):
        hub = AgentHub()
        agent = FakeSocket()
        browser = FakeSocket()
        hub.register(agent)
        event = {
            "type": "final_transcript",
            "requestId": "req-1",
            "sessionId": "session-1",
            "text": "我想吃饭",
            "timestamp": 1,
        }

        delivered = await hub.publish(event, browser)
        await hub.handle_message(
            agent,
            '{"type":"agent_result","requestId":"req-1","result":{"reply":"好"}}',
        )

        self.assertTrue(delivered)
        self.assertEqual(json.loads(agent.sent[0]), event)
        self.assertEqual(
            json.loads(browser.sent[0]),
            {
                "type": "agent_result",
                "requestId": "req-1",
                "result": {"reply": "好"},
            },
        )

    async def test_publish_reports_no_agent(self):
        hub = AgentHub()

        self.assertFalse(
            await hub.publish(
                {
                    "type": "final_transcript",
                    "requestId": "req-1",
                    "sessionId": "session-1",
                    "text": "我想吃饭",
                    "timestamp": 1,
                },
                FakeSocket(),
            )
        )

    async def test_closed_browser_does_not_break_agent_result_handling(self):
        hub = AgentHub()
        agent = FakeSocket()
        hub.pending["req-1"] = ClosedSocket()

        await hub.handle_message(
            agent,
            '{"type":"agent_result","requestId":"req-1","result":{"reply":"好"}}',
        )

        self.assertNotIn("req-1", hub.pending)


if __name__ == "__main__":
    unittest.main()
