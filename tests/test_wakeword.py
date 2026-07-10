import array
import tempfile
import unittest
from pathlib import Path

from wakeword import WakeWordDetector, WakeWordSetupError


MODEL_FILES = (
    "encoder-epoch-13-avg-2-chunk-8-left-64.int8.onnx",
    "decoder-epoch-13-avg-2-chunk-8-left-64.onnx",
    "joiner-epoch-13-avg-2-chunk-8-left-64.int8.onnx",
    "tokens.txt",
    "keywords.txt",
)


class FakeStream:
    def __init__(self):
        self.frames = []

    def accept_waveform(self, sample_rate, samples):
        self.frames.append((sample_rate, samples))


class FakeKeywordSpotter:
    def __init__(self, results, **kwargs):
        self.kwargs = kwargs
        self.results = list(results)
        self.streams = []
        self.reset_count = 0

    def create_stream(self):
        stream = FakeStream()
        self.streams.append(stream)
        return stream

    def is_ready(self, stream):
        return bool(stream.frames)

    def decode_stream(self, stream):
        stream.frames.pop(0)

    def get_result(self, stream):
        return self.results.pop(0) if self.results else ""

    def reset_stream(self, stream):
        self.reset_count += 1


class FakeSherpa:
    def __init__(self, results=()):
        self.results = results
        self.instances = []

    def KeywordSpotter(self, **kwargs):
        instance = FakeKeywordSpotter(self.results, **kwargs)
        self.instances.append(instance)
        return instance


def quiet_pcm(milliseconds=100):
    return array.array("h", [0] * (16 * milliseconds)).tobytes()


class WakeWordTests(unittest.TestCase):
    def create_model_dir(self, root):
        model_dir = Path(root)
        for filename in MODEL_FILES:
            (model_dir / filename).write_bytes(b"test")
        return model_dir

    def test_missing_model_files_gives_setup_instruction(self):
        with tempfile.TemporaryDirectory() as root:
            with self.assertRaisesRegex(WakeWordSetupError, "setup_kws"):
                WakeWordDetector(Path(root), sherpa_module=FakeSherpa())

    def test_detector_uses_low_latency_int8_model(self):
        with tempfile.TemporaryDirectory() as root:
            model_dir = self.create_model_dir(root)
            fake = FakeSherpa()

            WakeWordDetector(model_dir, sherpa_module=fake)

        kwargs = fake.instances[0].kwargs
        self.assertTrue(kwargs["encoder"].endswith("chunk-8-left-64.int8.onnx"))
        self.assertTrue(kwargs["joiner"].endswith("chunk-8-left-64.int8.onnx"))
        self.assertEqual(kwargs["provider"], "cpu")

    def test_hit_returns_true_and_resets_stream(self):
        with tempfile.TemporaryDirectory() as root:
            model_dir = self.create_model_dir(root)
            fake = FakeSherpa(["小瓜小瓜"])
            detector = WakeWordDetector(model_dir, sherpa_module=fake)

            hit = detector.accept(quiet_pcm())

        self.assertTrue(hit)
        self.assertEqual(fake.instances[0].reset_count, 1)

    def test_non_hit_returns_false(self):
        with tempfile.TemporaryDirectory() as root:
            model_dir = self.create_model_dir(root)
            detector = WakeWordDetector(model_dir, sherpa_module=FakeSherpa([""]))

            self.assertFalse(detector.accept(quiet_pcm()))

    def test_reset_replaces_session_stream(self):
        with tempfile.TemporaryDirectory() as root:
            model_dir = self.create_model_dir(root)
            fake = FakeSherpa()
            detector = WakeWordDetector(model_dir, sherpa_module=fake)
            first_stream = detector.stream

            detector.reset()

        self.assertIsNot(detector.stream, first_stream)


if __name__ == "__main__":
    unittest.main()
