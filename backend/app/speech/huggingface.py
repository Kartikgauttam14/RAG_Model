import httpx

from app.config import Settings
from app.speech.base import SynthesizedAudio, Transcript

# Speech calls are long running, so the timeout is applied per request as well as
# on the internally created client (a client supplied by dependency injection does
# not carry this provider's timeout).
SPEECH_TIMEOUT_SECONDS = 60


class SpeechProviderUnavailableError(RuntimeError):
    pass


class HuggingFaceSTTProvider:
    def __init__(self, settings: Settings, client: httpx.AsyncClient | None = None) -> None:
        if not settings.stt_inference_url:
            raise ValueError("STT_INFERENCE_URL is required")
        self.settings = settings
        self.endpoint = settings.stt_inference_url
        self.client = client or httpx.AsyncClient(timeout=SPEECH_TIMEOUT_SECONDS)

    async def transcribe(self, audio: bytes, media_type: str, language: str | None = None) -> Transcript:
        token = self.settings.stt_api_key or self.settings.hf_token
        headers = {"Content-Type": media_type}
        if token:
            headers["Authorization"] = f"Bearer {token}"
        try:
            response = await self.client.post(
                self.endpoint,
                headers=headers,
                content=audio,
                params={"language": language} if language else None,
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


class HuggingFaceTTSProvider:
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
                    "inputs": text,
                    "parameters": {
                        "language": language,
                        "voice": selected_voice,
                        "speed": speed or self.settings.tts_speed,
                    },
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
