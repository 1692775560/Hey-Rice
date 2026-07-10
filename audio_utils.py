"""Pure PCM helpers and the local utterance end detector."""
from __future__ import annotations

import array
import re
import sys
from enum import Enum


class EndReason(str, Enum):
    SILENCE = "silence"
    NO_SPEECH = "no_speech"
    MAX_DURATION = "max_duration"


def _pcm_samples(pcm: bytes) -> array.array:
    if len(pcm) % 2:
        raise ValueError("PCM16 data must be aligned to 2-byte samples")
    samples = array.array("h")
    samples.frombytes(pcm)
    if sys.byteorder != "little":
        samples.byteswap()
    return samples


def pcm_rms(pcm: bytes) -> float:
    samples = _pcm_samples(pcm)
    if not samples:
        return 0.0
    return (sum(sample * sample for sample in samples) / len(samples)) ** 0.5


class UtteranceDetector:
    def __init__(
        self,
        *,
        sample_rate: int = 16000,
        speech_threshold: float = 500.0,
        silence_ms: int = 900,
        no_speech_ms: int = 4000,
        max_duration_ms: int = 12000,
    ) -> None:
        self.sample_rate = sample_rate
        self.speech_threshold = speech_threshold
        self.silence_ms = silence_ms
        self.no_speech_ms = no_speech_ms
        self.max_duration_ms = max_duration_ms
        self.reset()

    def reset(self) -> None:
        self.elapsed_ms = 0.0
        self.trailing_silence_ms = 0.0
        self.speech_started = False

    def feed(self, pcm: bytes) -> EndReason | None:
        samples = _pcm_samples(pcm)
        frame_ms = len(samples) * 1000 / self.sample_rate
        self.elapsed_ms += frame_ms

        if pcm_rms(pcm) >= self.speech_threshold:
            self.speech_started = True
            self.trailing_silence_ms = 0.0
        elif self.speech_started:
            self.trailing_silence_ms += frame_ms

        if self.elapsed_ms >= self.max_duration_ms:
            return EndReason.MAX_DURATION
        if not self.speech_started and self.elapsed_ms >= self.no_speech_ms:
            return EndReason.NO_SPEECH
        if self.speech_started and self.trailing_silence_ms >= self.silence_ms:
            return EndReason.SILENCE
        return None


_LEADING_SEPARATOR = re.compile(r"^[\s,，。.!！？:：;；、]+")


def normalize_transcript(text: str, wake_word: str) -> str:
    normalized = text.strip()
    if normalized.startswith(wake_word):
        normalized = normalized[len(wake_word):]
        normalized = _LEADING_SEPARATOR.sub("", normalized)
    return normalized.strip()
