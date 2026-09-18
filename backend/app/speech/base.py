from dataclasses import dataclass
from typing import Protocol


@dataclass(frozen=True)
class Transcript:
    text: str
    confidence: float | None
    language: str | None


@dataclass(frozen=True)
class SynthesizedAudio:
    content: bytes
    media_type: str
    voice: str


class SpeechToTextProvider(Protocol):
    async def transcribe(self, audio: bytes, media_type: str, language: str | None) -> Transcript: ...


class TextToSpeechProvider(Protocol):
    async def synthesize(
        self, text: str, language: str, voice: str | None = None, speed: float | None = None
    ) -> SynthesizedAudio: ...
