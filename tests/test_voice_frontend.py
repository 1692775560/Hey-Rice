import unittest
from pathlib import Path

from server import resolve_static_asset


ROOT = Path(__file__).resolve().parents[1]


class VoiceFrontendTests(unittest.TestCase):
    def test_page_contains_voice_controls(self):
        html = (ROOT / "index.html").read_text(encoding="utf-8")

        self.assertIn('id="micToggle"', html)
        self.assertIn('id="voiceStatus"', html)
        self.assertIn('id="voiceTranscript"', html)
        self.assertIn('/static/voice-client.js', html)

    def test_server_serves_voice_client_as_javascript(self):
        asset = resolve_static_asset("/static/voice-client.js")
        path, content_type = asset
        body = path.read_text(encoding="utf-8")

        self.assertIn("class VoiceClient", body)
        self.assertEqual(content_type, "text/javascript; charset=utf-8")

    def test_server_serves_audio_worklet_as_javascript(self):
        asset = resolve_static_asset("/static/pcm-worklet.js")
        path, _ = asset
        body = path.read_text(encoding="utf-8")

        self.assertIn("registerProcessor", body)

    def test_static_path_traversal_is_not_served(self):
        self.assertIsNone(resolve_static_asset("/static/../config.py"))


if __name__ == "__main__":
    unittest.main()
