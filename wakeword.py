"""On-device wake word detection backed by sherpa-onnx."""
from __future__ import annotations

from pathlib import Path
from types import ModuleType


ENCODER = "encoder-epoch-13-avg-2-chunk-8-left-64.int8.onnx"
DECODER = "decoder-epoch-13-avg-2-chunk-8-left-64.onnx"
JOINER = "joiner-epoch-13-avg-2-chunk-8-left-64.int8.onnx"
TOKENS = "tokens.txt"
KEYWORDS = "keywords.txt"
REQUIRED_FILES = (ENCODER, DECODER, JOINER, TOKENS, KEYWORDS)


class WakeWordSetupError(RuntimeError):
    pass


class WakeWordDetector:
    def __init__(
        self,
        model_dir: str | Path,
        *,
        sample_rate: int = 16000,
        sherpa_module: ModuleType | object | None = None,
    ) -> None:
        self.model_dir = Path(model_dir)
        self.sample_rate = sample_rate
        missing = [name for name in REQUIRED_FILES if not (self.model_dir / name).is_file()]
        if missing:
            raise WakeWordSetupError(
                "KWS model is incomplete; run `python scripts/setup_kws.py`. "
                f"Missing: {', '.join(missing)}"
            )

        if sherpa_module is None:
            try:
                import sherpa_onnx as sherpa_module
            except ImportError as exc:
                raise WakeWordSetupError(
                    "sherpa-onnx is not installed; run `pip install -r requirements.txt`"
                ) from exc

        self.engine = sherpa_module.KeywordSpotter(
            tokens=str(self.model_dir / TOKENS),
            encoder=str(self.model_dir / ENCODER),
            decoder=str(self.model_dir / DECODER),
            joiner=str(self.model_dir / JOINER),
            num_threads=2,
            keywords_file=str(self.model_dir / KEYWORDS),
            provider="cpu",
        )
        self.stream = self.engine.create_stream()

    def reset(self) -> None:
        self.stream = self.engine.create_stream()

    def accept(self, pcm: bytes) -> bool:
        if len(pcm) % 2:
            raise ValueError("PCM16 data must be aligned to 2-byte samples")
        try:
            import numpy as np
        except ImportError as exc:
            raise WakeWordSetupError(
                "numpy is not installed; run `pip install -r requirements.txt`"
            ) from exc

        samples = np.frombuffer(pcm, dtype="<i2").astype(np.float32) / 32768.0
        self.stream.accept_waveform(self.sample_rate, samples)
        while self.engine.is_ready(self.stream):
            self.engine.decode_stream(self.stream)
            if self.engine.get_result(self.stream):
                self.engine.reset_stream(self.stream)
                return True
        return False
