import httpx
import pytest
import respx
from app.config import Settings
from app.speech.huggingface import SpeechProviderUnavailableError
from app.speech.openai_compatible import OpenAICompatibleSTTProvider, OpenAICompatibleTTSProvider

STT_ENDPOINT = "http://127.0.0.1:8083/v1/audio/transcriptions"
TTS_ENDPOINT = "http://127.0.0.1:8083/v1/audio/speech"


def _stt_settings() -> Settings:
    return Settings(
        stt_provider="openai",
        stt_model="Systran/faster-whisper-small",
        stt_inference_url=STT_ENDPOINT,
    )


@respx.mock
@pytest.mark.asyncio
async def test_openai_stt_posts_multipart_file_and_truncates_language_tag() -> None:
    route = respx.post(STT_ENDPOINT).mock(return_value=httpx.Response(200, json={"text": "ما مدة التوصيل؟"}))
    settings = _stt_settings()

    async with httpx.AsyncClient() as client:
        result = await OpenAICompatibleSTTProvider(settings, client).transcribe(
            b"audio-bytes", "audio/webm", "ar-SA"
        )

    assert result.text == "ما مدة التوصيل؟"
    assert result.language == "ar-SA"
    request = route.calls[0].request
    body = request.content.decode("utf-8", errors="ignore")
    assert 'name="model"' in body
    assert "Systran/faster-whisper-small" in body
    # Whisper expects an ISO-639-1 code, not the full "ar-SA" tag the browser sends.
    assert 'name="language"' in body
    assert "ar-SA" not in body
    assert "audio-bytes" in body


@respx.mock
@pytest.mark.asyncio
async def test_openai_stt_sends_the_vocabulary_prompt_when_configured() -> None:
    route = respx.post(STT_ENDPOINT).mock(return_value=httpx.Response(200, json={"text": "the price of Mamlakati"}))
    settings = Settings(
        stt_provider="openai",
        stt_model="Systran/faster-whisper-small",
        stt_inference_url=STT_ENDPOINT,
        stt_prompt="Mansam fragrance catalogue: Mamlakati, Qanun.",
    )

    async with httpx.AsyncClient() as client:
        await OpenAICompatibleSTTProvider(settings, client).transcribe(b"audio", "audio/webm")

    body = route.calls[0].request.content.decode("utf-8", errors="ignore")
    assert 'name="prompt"' in body
    assert "Mamlakati, Qanun" in body


@respx.mock
@pytest.mark.asyncio
async def test_openai_stt_reports_empty_transcript_as_unavailable() -> None:
    respx.post(STT_ENDPOINT).mock(return_value=httpx.Response(200, json={"text": "  "}))

    async with httpx.AsyncClient() as client:
        with pytest.raises(SpeechProviderUnavailableError):
            await OpenAICompatibleSTTProvider(_stt_settings(), client).transcribe(b"audio", "audio/webm")


@respx.mock
@pytest.mark.asyncio
async def test_openai_tts_sends_voice_and_returns_wav_bytes() -> None:
    route = respx.post(TTS_ENDPOINT).mock(
        return_value=httpx.Response(200, content=b"RIFF....WAVE", headers={"content-type": "audio/wav"})
    )
    settings = Settings(
        tts_provider="openai",
        tts_model="speaches-ai/Kokoro-82M-v1.0-ONNX",
        tts_inference_url=TTS_ENDPOINT,
        tts_voice="af_heart",
    )

    async with httpx.AsyncClient() as client:
        audio = await OpenAICompatibleTTSProvider(settings, client).synthesize("Hello", "en")

    assert audio.content == b"RIFF....WAVE"
    assert audio.media_type == "audio/wav"
    assert audio.voice == "af_heart"
    payload = route.calls[0].request.content.decode()
    assert "speaches-ai/Kokoro-82M-v1.0-ONNX" in payload
    assert "af_heart" in payload
