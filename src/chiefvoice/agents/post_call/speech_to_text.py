"""Recording transcription through Google Cloud Speech-to-Text Chirp 2."""

from __future__ import annotations

import asyncio
import io
import wave

_CHUNK_SECONDS = 50


def _transcribe_wav_sync(
    audio: bytes, language: str, project_id: str | None, location: str, model: str
) -> str:
    import google.auth
    from google.api_core.client_options import ClientOptions
    from google.cloud.speech_v2 import SpeechClient
    from google.cloud.speech_v2.types import cloud_speech

    if not project_id:
        _, project_id = google.auth.default()
    if not project_id:
        raise RuntimeError("Speech-to-Text project is not configured")

    normalized_language = language.strip() if language else "auto"
    if normalized_language and normalized_language.lower() not in {"auto", "unknown"}:
        # Cloud Speech expects BCP-47 codes; accept the base language for common inputs.
        normalized_language = {
            "ta": "ta-IN",
            "en": "en-IN",
            "hi": "hi-IN",
            "te": "te-IN",
            "ml": "ml-IN",
        }.get(normalized_language.lower(), normalized_language)
    else:
        normalized_language = "auto"

    transcript = ""
    with SpeechClient(
        client_options=ClientOptions(api_endpoint=f"{location}-speech.googleapis.com")
    ) as client:
        with wave.open(io.BytesIO(audio), "rb") as wav:
            params = wav.getparams()
            if params.comptype != "NONE":
                raise ValueError("Recording WAV must contain uncompressed PCM audio")
            frames_per_chunk = max(1, params.framerate * _CHUNK_SECONDS)
            while frames := wav.readframes(frames_per_chunk):
                buffer = io.BytesIO()
                with wave.open(buffer, "wb") as chunk_wav:
                    chunk_wav.setnchannels(params.nchannels)
                    chunk_wav.setsampwidth(params.sampwidth)
                    chunk_wav.setframerate(params.framerate)
                    chunk_wav.writeframes(frames)
                config = cloud_speech.RecognitionConfig(
                    auto_decoding_config=cloud_speech.AutoDetectDecodingConfig(),
                    language_codes=[normalized_language],
                    model=model,
                    features=cloud_speech.RecognitionFeatures(
                        enable_automatic_punctuation=True,
                    ),
                )
                request = cloud_speech.RecognizeRequest(
                    recognizer=f"projects/{project_id}/locations/{location}/recognizers/_",
                    config=config,
                    content=buffer.getvalue(),
                )
                response = client.recognize(request=request, timeout=90)
                for result in response.results:
                    if result.alternatives:
                        transcript = " ".join(
                            part
                            for part in (transcript, result.alternatives[0].transcript.strip())
                            if part
                        )
    return transcript


async def transcribe_recording(
    audio: bytes,
    *,
    language: str | None,
    project_id: str | None,
    location: str = "asia-southeast1",
    model: str = "chirp_2",
) -> str:
    """Transcribe a WAV recording without blocking the post-call event loop."""
    return await asyncio.to_thread(
        _transcribe_wav_sync,
        audio,
        language or "auto",
        project_id,
        location,
        model,
    )
