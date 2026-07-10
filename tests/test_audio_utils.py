import array
import unittest

from audio_utils import EndReason, UtteranceDetector, normalize_transcript, pcm_rms


def pcm_frame(amplitude: int, milliseconds: int = 100) -> bytes:
    samples = array.array("h", [amplitude] * (16 * milliseconds))
    return samples.tobytes()


class PcmTests(unittest.TestCase):
    def test_rms_uses_signed_pcm16_samples(self):
        self.assertEqual(pcm_rms(pcm_frame(1200)), 1200.0)

    def test_rms_rejects_partial_sample(self):
        with self.assertRaisesRegex(ValueError, "aligned"):
            pcm_rms(b"\x01")


class UtteranceDetectorTests(unittest.TestCase):
    def test_silence_before_speech_does_not_finish_utterance(self):
        detector = UtteranceDetector(silence_ms=300, no_speech_ms=1000)

        self.assertIsNone(detector.feed(pcm_frame(0, 500)))

    def test_trailing_silence_finishes_after_speech(self):
        detector = UtteranceDetector(silence_ms=300, no_speech_ms=1000)

        self.assertIsNone(detector.feed(pcm_frame(2000, 100)))
        self.assertIsNone(detector.feed(pcm_frame(0, 200)))
        self.assertEqual(detector.feed(pcm_frame(0, 100)), EndReason.SILENCE)

    def test_no_speech_timeout_finishes_without_voice(self):
        detector = UtteranceDetector(silence_ms=300, no_speech_ms=400)

        self.assertEqual(detector.feed(pcm_frame(0, 400)), EndReason.NO_SPEECH)

    def test_max_duration_finishes_even_during_speech(self):
        detector = UtteranceDetector(max_duration_ms=500)

        self.assertEqual(detector.feed(pcm_frame(2000, 500)), EndReason.MAX_DURATION)

    def test_reset_clears_prior_speech_state(self):
        detector = UtteranceDetector(silence_ms=200, no_speech_ms=500)
        detector.feed(pcm_frame(2000, 100))

        detector.reset()

        self.assertIsNone(detector.feed(pcm_frame(0, 200)))


class TranscriptTests(unittest.TestCase):
    def test_removes_leading_wake_word_and_separator(self):
        self.assertEqual(
            normalize_transcript("小瓜小瓜，我想吃饭。", "小瓜小瓜"),
            "我想吃饭。",
        )

    def test_preserves_wake_word_when_not_at_start(self):
        self.assertEqual(
            normalize_transcript("我在叫小瓜小瓜", "小瓜小瓜"),
            "我在叫小瓜小瓜",
        )

    def test_blank_after_wake_word_is_empty(self):
        self.assertEqual(normalize_transcript("小瓜小瓜！", "小瓜小瓜"), "")


if __name__ == "__main__":
    unittest.main()
