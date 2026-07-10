"""Environment-backed configuration for the optional voice gateway."""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path


HERE = Path(__file__).resolve().parent
DEFAULT_KWS_MODEL = HERE / "models" / "sherpa-onnx-kws-zipformer-zh-en-3M-2025-12-20"


def _bool_env(name: str, default: bool) -> bool:
    raw = os.environ.get(name)
    if raw is None:
        return default
    return raw.strip().lower() not in {"0", "false", "no", "off"}


@dataclass(frozen=True)
class VoiceConfig:
    voice_enabled: bool
    websocket_host: str
    websocket_port: int
    wake_word: str
    sample_rate: int
    kws_model_dir: Path
    app_id: str = field(repr=False)
    access_token: str = field(repr=False)
    secret_key: str = field(repr=False)
    resource_id: str
    asr_endpoint: str
    silence_ms: int
    no_speech_ms: int
    max_duration_ms: int
    speech_threshold: float
    embedded_agent: bool

    @classmethod
    def from_env(cls) -> "VoiceConfig":
        return cls(
            voice_enabled=_bool_env("MEALMATE_VOICE_ENABLED", True),
            websocket_host=os.environ.get("MEALMATE_VOICE_HOST", "127.0.0.1"),
            websocket_port=int(os.environ.get("MEALMATE_VOICE_PORT", "8765")),
            wake_word=os.environ.get("MEALMATE_WAKE_WORD", "小瓜小瓜"),
            sample_rate=16000,
            kws_model_dir=Path(
                os.environ.get("KWS_MODEL_DIR", str(DEFAULT_KWS_MODEL))
            ).expanduser(),
            app_id=os.environ.get("DOUBAO_APP_ID", ""),
            access_token=os.environ.get("DOUBAO_ACCESS_TOKEN", ""),
            secret_key=os.environ.get("DOUBAO_SECRET_KEY", ""),
            resource_id=os.environ.get(
                "DOUBAO_RESOURCE_ID", "volc.bigasr.sauc.duration"
            ),
            asr_endpoint=os.environ.get(
                "DOUBAO_ASR_ENDPOINT",
                "wss://openspeech.bytedance.com/api/v3/sauc/bigmodel_async",
            ),
            silence_ms=int(os.environ.get("MEALMATE_SILENCE_MS", "900")),
            no_speech_ms=int(os.environ.get("MEALMATE_NO_SPEECH_MS", "4000")),
            max_duration_ms=int(os.environ.get("MEALMATE_MAX_UTTERANCE_MS", "12000")),
            speech_threshold=float(
                os.environ.get("MEALMATE_SPEECH_THRESHOLD", "500")
            ),
            embedded_agent=_bool_env("MEALMATE_EMBED_AGENT", True),
        )

    def missing_requirements(self) -> list[str]:
        if not self.voice_enabled:
            return []
        missing = []
        if not self.app_id:
            missing.append("DOUBAO_APP_ID")
        if not self.access_token:
            missing.append("DOUBAO_ACCESS_TOKEN")
        if not self.kws_model_dir.is_dir():
            missing.append("KWS_MODEL_DIR")
        return missing
