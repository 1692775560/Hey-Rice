import unittest

from agent_ws_client import process_agent_event
from voice_protocol import ProtocolError


class AgentWsClientTests(unittest.IsolatedAsyncioTestCase):
    async def test_final_transcript_calls_existing_agent_handler(self):
        received = []

        def handler(text):
            received.append(text)
            return {"reply": "好嘞", "intent": "FEED_FIRST"}

        response = await process_agent_event(
            '{"type":"final_transcript","requestId":"req-1",'
            '"sessionId":"session-1","text":"我想吃饭","timestamp":1234}',
            handler,
        )

        self.assertEqual(received, ["我想吃饭"])
        self.assertEqual(
            response,
            {
                "type": "agent_result",
                "requestId": "req-1",
                "result": {"reply": "好嘞", "intent": "FEED_FIRST"},
            },
        )

    async def test_invalid_event_never_calls_handler(self):
        called = False

        def handler(text):
            nonlocal called
            called = True

        with self.assertRaises(ProtocolError):
            await process_agent_event('{"type":"partial","text":"我想"}', handler)
        self.assertFalse(called)

    async def test_handler_failure_returns_safe_error(self):
        def handler(text):
            raise RuntimeError("private backend detail")

        response = await process_agent_event(
            '{"type":"final_transcript","requestId":"req-1",'
            '"sessionId":"session-1","text":"我想吃饭","timestamp":1234}',
            handler,
        )

        self.assertEqual(response["requestId"], "req-1")
        self.assertEqual(response["result"], {"error": "agent processing failed"})
        self.assertNotIn("private backend detail", repr(response))


if __name__ == "__main__":
    unittest.main()
