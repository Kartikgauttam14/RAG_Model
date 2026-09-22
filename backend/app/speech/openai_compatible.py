"""Speech providers that speak the OpenAI audio API.

A local speech server (speaches, faster-whisper-server, whisper.cpp's OpenAI route,
or the OpenAI API itself) exposes ``POST /v1/audio/transcriptions`` and
``POST /v1/audio/speech``. Those two endpoints let the microphone and the playback
controls work from a self-hosted model instead of a hosted token, which is what the
bundled compose ``speech`` service provides. The Hugging Face provider stays the
default so existing deployments are unaffected.
"""

import httpx

from app.config import Settings
from app.speech.base import SynthesizedAudio, Transcript
from app.speech.huggingface import SPEECH_TIMEOUT_SECONDS, SpeechProviderUnavailableError


class OpenAICompatibleSTTProvider:
    """Speech-to-text through the OpenAI multipart transcription contract."""

    def __init__(self, settings: Settings, client: httpx.AsyncClient | None = None) -> None:
        if not settings.stt_inference_url:
            raise ValueError("STT_INFERENCE_URL is required")
        self.settings = settings
        self.endpoint = settings.stt_inference_url
        self.client = client or httpx.AsyncClient(timeout=SPEECH_TIMEOUT_SECONDS)

    async def transcribe(self, audio: bytes, media_type: str, language: str | None = None) -> Transcript:
        token = self.settings.stt_api_key or self.settings.hf_token
        headers = {"Authorization": f"Bearer {token}"} if token else {}
        fields = {"model": self.settings.stt_model, "response_format": "json"}
        # Whisper biases decoding toward the words in this prompt, so sending the
        # catalogue vocabulary is what keeps brand names spelled correctly.
        if self.settings.stt_prompt:
            fields["prompt"] = self.settings.stt_prompt
        # Whisper wants an ISO-639-1 code; the UI may send a full tag such as "ar-SA".
        if language:
            fields["language"] = language.split("-")[0][:2]
        try:
            response = await self.client.post(
                self.endpoint,
                headers=headers,
                data=fields,
                files={"file": ("recording.webm", audio, media_type or "audio/webm")},
                timeout=SPEECH_TIMEOUT_SECONDS,
            )
            response.raise_for_status()
            body = response.json()
            text = str(body.get("text", "")).strip()
            if not text:
                raise SpeechProviderUnavailableError("STT returned an empty transcript")
            confidence = body.get("confidence")
            return Transcript(
                text=text,
                confidence=float(confidence) if confidence is not None else None,
                language=body.get("language", language),
            )
        except (httpx.HTTPError, ValueError, TypeError) as exc:
            if isinstance(exc, SpeechProviderUnavailableError):
                raise
            raise SpeechProviderUnavailableError("Speech recognition is unavailable") from exc


class OpenAICompatibleTTSProvider:
    """Text-to-speech through the OpenAI speech synthesis contract."""

    def __init__(self, settings: Settings, client: httpx.AsyncClient | None = None) -> None:
        if not settings.tts_inference_url:
            raise ValueError("TTS_INFERENCE_URL is required")
        self.settings = settings
        self.endpoint = settings.tts_inference_url
        self.client = client or httpx.AsyncClient(timeout=SPEECH_TIMEOUT_SECONDS)

    async def synthesize(
        self, text: str, language: str, voice: str | None = None, speed: float | None = None
    ) -> SynthesizedAudio:
        token = self.settings.tts_api_key or self.settings.hf_token
        headers = {"Authorization": f"Bearer {token}"} if token else {}
        selected_voice = voice or self.settings.tts_voice
        try:
            response = await self.client.post(
                self.endpoint,
                headers=headers,
                json={
                    "model": self.settings.tts_model,
                    "input": text,
                    "voice": selected_voice,
                    "response_format": "wav",
                    "speed": speed or self.settings.tts_speed,
                },
                timeout=SPEECH_TIMEOUT_SECONDS,
            )
            response.raise_for_status()
            media_type = response.headers.get("content-type", "audio/wav").split(";")[0]
            if not response.content:
                raise SpeechProviderUnavailableError("TTS returned empty audio")
            return SynthesizedAudio(response.content, media_type, selected_voice)
        except httpx.HTTPError as exc:
            raise SpeechProviderUnavailableError("Speech synthesis is unavailable") from exc
