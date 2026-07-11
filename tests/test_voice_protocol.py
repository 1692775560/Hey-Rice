import json
import unittest

from voice_protocol import (
    ProtocolError,
    build_agent_transcript,
    parse_agent_result,
    parse_agent_transcript,
    parse_browser_control,
)


class BrowserControlTests(unittest.TestCase):
    def test_start_requires_16khz_audio(self):
        message = parse_browser_control('{"type":"start","sampleRate":16000}')

        self.assertEqual(message.type, "start")
        self.assertEqual(message.sample_rate, 16000)

    def test_start_rejects_wrong_sample_rate(self):
        with self.assertRaisesRegex(ProtocolError, "16000"):
            parse_browser_control('{"type":"start","sampleRate":48000}')

    def test_control_rejects_unknown_type(self):
        with self.assertRaisesRegex(ProtocolError, "unsupported"):
            parse_browser_control('{"type":"wake"}')

    def test_push_to_talk_controls_are_accepted(self):
        for kind in ("speak_start", "speak_end", "stop"):
            message = parse_browser_control(json.dumps({"type": kind}))
            self.assertEqual(message.type, kind)

    def test_control_rejects_non_object_json(self):
        with self.assertRaisesRegex(ProtocolError, "object"):
            parse_browser_control('["start"]')


class AgentContractTests(unittest.TestCase):
    def test_transcript_event_contains_only_contract_fields(self):
        event = build_agent_transcript(
            request_id="req-1",
            session_id="session-1",
            text="我想吃饭",
            timestamp_ms=1234,
        )

        self.assertEqual(
            event,
            {
                "type": "final_transcript",
                "requestId": "req-1",
                "sessionId": "session-1",
                "text": "我想吃饭",
                "timestamp": 1234,
            },
        )

    def test_transcript_event_rejects_blank_text(self):
        with self.assertRaisesRegex(ProtocolError, "text"):
            build_agent_transcript("req-1", "session-1", "   ", 1234)

    def test_agent_result_requires_request_id_and_object_result(self):
        parsed = parse_agent_result(
            json.dumps(
                {
                    "type": "agent_result",
                    "requestId": "req-1",
                    "result": {"reply": "好"},
                }
            )
        )

        self.assertEqual(parsed.request_id, "req-1")
        self.assertEqual(parsed.result, {"reply": "好"})

    def test_agent_result_rejects_wrong_shape(self):
        with self.assertRaises(ProtocolError):
            parse_agent_result(
                '{"type":"agent_result","requestId":"req-1","result":"bad"}'
            )

    def test_agent_transcript_parser_accepts_final_text(self):
        parsed = parse_agent_transcript(
            '{"type":"final_transcript","requestId":"req-1",'
            '"sessionId":"session-1","text":"我想吃饭","timestamp":1234}'
        )

        self.assertEqual(parsed.request_id, "req-1")
        self.assertEqual(parsed.session_id, "session-1")
        self.assertEqual(parsed.text, "我想吃饭")

    def test_agent_transcript_parser_rejects_blank_text(self):
        with self.assertRaisesRegex(ProtocolError, "text"):
            parse_agent_transcript(
                '{"type":"final_transcript","requestId":"req-1",'
                '"sessionId":"session-1","text":" ","timestamp":1234}'
            )


if __name__ == "__main__":
    unittest.main()
