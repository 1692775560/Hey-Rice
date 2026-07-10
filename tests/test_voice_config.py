import os
import unittest
from unittest.mock import patch

from voice_config import VoiceConfig


class VoiceConfigTests(unittest.TestCase):
    def test_defaults_match_public_voice_contract(self):
        with patch.dict(os.environ, {}, clear=True):
            config = VoiceConfig.from_env()

        self.assertEqual(config.wake_word, "小瓜小瓜")
        self.assertEqual(config.websocket_port, 8765)
        self.assertEqual(config.sample_rate, 16000)
        self.assertTrue(config.voice_enabled)

    def test_reports_missing_runtime_requirements(self):
        # 显式指向不存在的模型目录,使断言不依赖本机是否已下载唤醒模型。
        with patch.dict(os.environ, {"KWS_MODEL_DIR": "/nonexistent/kws-model"}, clear=True):
            config = VoiceConfig.from_env()

        self.assertEqual(
            config.missing_requirements(),
            ["DOUBAO_APP_ID", "DOUBAO_ACCESS_TOKEN", "KWS_MODEL_DIR"],
        )

    def test_secret_values_are_hidden_from_repr(self):
        with patch.dict(
            os.environ,
            {
                "DOUBAO_APP_ID": "app-value",
                "DOUBAO_ACCESS_TOKEN": "token-value",
                "DOUBAO_SECRET_KEY": "secret-value",
                "KWS_MODEL_DIR": "/tmp/model",
            },
            clear=True,
        ):
            rendered = repr(VoiceConfig.from_env())

        self.assertNotIn("token-value", rendered)
        self.assertNotIn("secret-value", rendered)

    def test_voice_can_be_disabled_without_credentials(self):
        with patch.dict(os.environ, {"MEALMATE_VOICE_ENABLED": "0"}, clear=True):
            config = VoiceConfig.from_env()

        self.assertFalse(config.voice_enabled)
        self.assertEqual(config.missing_requirements(), [])


if __name__ == "__main__":
    unittest.main()
