"""Plain text-to-speech endpoints.

These are thin wrappers around the XTTS service — useful for testing
and as a building block the extension can use directly when the user
already has an English transcript and just wants Russian audio."""

from __future__ import annotations

import io

from fastapi import APIRouter, HTTPException, UploadFile
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field

from app.services import xtts

router = APIRouter(prefix="/tts", tags=["tts"])


class TTSRequest(BaseModel):
    text: str = Field(..., min_length=1, max_length=2000)
    language: str = Field("ru", min_length=2, max_length=5)
    speaker: str | None = Field(None, description="Built-in XTTS speaker name.")


@router.post("", summary="Synthesize text to speech (built-in voice)")
async def synthesize(req: TTSRequest):
    try:
        samples, sr = xtts.synthesize_to_array(
            req.text, language=req.language, speaker=req.speaker
        )
    except Exception as e:  # pragma: no cover - exercised on real model only
        raise HTTPException(500, f"TTS failed: {e}") from e

    buf = io.BytesIO()
    import soundfile as sf

    sf.write(buf, samples, sr, format="WAV", subtype="PCM_16")
    buf.seek(0)
    return StreamingResponse(buf, media_type="audio/wav")


@router.post("/clone", summary="Synthesize with voice cloning from a reference clip")
async def synthesize_clone(
    text: str,
    speaker_wav: UploadFile,
    language: str = "ru",
):
    if not speaker_wav.content_type or not speaker_wav.content_type.startswith("audio/"):
        raise HTTPException(415, "speaker_wav must be an audio/* file")
    import tempfile
    from pathlib import Path

    with tempfile.NamedTemporaryFile(delete=False, suffix=Path(speaker_wav.filename or "ref.wav").suffix) as tf:
        tf.write(await speaker_wav.read())
        ref_path = tf.name

    try:
        samples, sr = xtts.synthesize_to_array(
            text, language=language, speaker_wav=ref_path
        )
    except Exception as e:  # pragma: no cover
        raise HTTPException(500, f"TTS clone failed: {e}") from e
    finally:
        Path(ref_path).unlink(missing_ok=True)

    import soundfile as sf

    buf = io.BytesIO()
    sf.write(buf, samples, sr, format="WAV", subtype="PCM_16")
    buf.seek(0)
    return StreamingResponse(buf, media_type="audio/wav")
