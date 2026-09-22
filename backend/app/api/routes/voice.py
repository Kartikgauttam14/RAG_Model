import base64

from fastapi import APIRouter, Depends, File, Form, HTTPException, UploadFile
from pydantic import BaseModel, Field

from app.auth import Principal, get_current_principal
from app.config import Settings, get_settings
from app.dependencies import get_stt, get_tts
from app.speech import SpeechProviderUnavailableError, SpeechToTextProvider, TextToSpeechProvider

router = APIRouter(prefix="/voice", tags=["voice"])


class TranscriptResponse(BaseModel):
    transcript: str
    confidence: float | None
    language: str | None
    status: str


class SynthesizeRequest(BaseModel):
    text: str = Field(min_length=1, max_length=5000)
    language: str = Field(default="en", max_length=16)
    voice: str | None = Field(default=None, max_length=100)
    speed: float | None = Field(default=None, ge=0.5, le=2)


class AudioResponse(BaseModel):
    audio_base64: str
    media_type: str
    voice: str


@router.post("/transcribe", response_model=TranscriptResponse)
async def transcribe(
    audio: UploadFile = File(...),
    language: str | None = Form(None),
    _: Principal = Depends(get_current_principal),
    provider: SpeechToTextProvider = Depends(get_stt),
    settings: Settings = Depends(get_settings),
) -> TranscriptResponse:
    data = await audio.read()
    if not data or len(data) > settings.max_upload_size_mb * 1024 * 1024:
        raise HTTPException(status_code=413, detail="Audio is empty or too large")
    if not (audio.content_type or "").startswith("audio/"):
        raise HTTPException(status_code=415, detail="An audio media type is required")
    try:
        result = await provider.transcribe(data, audio.content_type or "audio/webm", language)
    except SpeechProviderUnavailableError as exc:
        # The provider exists but the speech server is down or has no model installed;
        # that is an unavailable dependency (503), not a server fault (500).
        raise HTTPException(status_code=503, detail="Speech recognition is unavailable") from exc
    low = result.confidence is not None and result.confidence < settings.stt_min_confidence
    return TranscriptResponse(
        transcript=result.text,
        confidence=result.confidence,
        language=result.language,
        status="needs_confirmation" if low else "ready",
    )


@router.post("/synthesize", response_model=AudioResponse)
async def synthesize(
    payload: SynthesizeRequest,
    _: Principal = Depends(get_current_principal),
    provider: TextToSpeechProvider = Depends(get_tts),
) -> AudioResponse:
    try:
        result = await provider.synthesize(payload.text, payload.language, payload.voice, payload.speed)
    except SpeechProviderUnavailableError as exc:
        raise HTTPException(status_code=503, detail="Speech synthesis is unavailable") from exc
    return AudioResponse(
        audio_base64=base64.b64encode(result.content).decode(),
        media_type=result.media_type,
        voice=result.voice,
    )
