"""Audio -> Whisper segments endpoint.

The extension itself doesn't strictly need this (it uses YouTube CC when
available); useful for debugging the model or for clients that need the
ASR output as a stand-alone product."""

from __future__ import annotations

import tempfile
from pathlib import Path

from fastapi import APIRouter, HTTPException, UploadFile

from app.services import whisper as whisper_svc

router = APIRouter(prefix="/transcribe", tags=["transcribe"])


@router.post("", summary="Transcribe an audio file with Faster-Whisper")
async def transcribe(file: UploadFile, language: str | None = "en"):
    if not file.content_type or not file.content_type.startswith(("audio/", "video/")):
        raise HTTPException(415, "file must be audio/* or video/*")

    suffix = Path(file.filename or "input.wav").suffix or ".wav"
    with tempfile.NamedTemporaryFile(delete=False, suffix=suffix) as tf:
        tf.write(await file.read())
        path = Path(tf.name)

    try:
        segments, detected = whisper_svc.transcribe_file(path, language=language)
    except Exception as e:  # pragma: no cover
        raise HTTPException(500, f"Whisper failed: {e}") from e

    return {
        "language": detected,
        "segments": whisper_svc.segments_as_dicts(segments),
    }
