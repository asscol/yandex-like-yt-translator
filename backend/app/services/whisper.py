"""Faster-Whisper wrapper.

We load the model lazily on first use and keep it in memory thereafter
(the FastAPI process is single-worker, so this is safe).  The Coqui /
HuggingFace caches are pointed at `MODEL_CACHE_DIR` so a Fly.io persistent
volume keeps the ~140 MB `base` model around between deploys.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path
from threading import Lock
from typing import TYPE_CHECKING, Iterable

if TYPE_CHECKING:  # pragma: no cover
    from faster_whisper import WhisperModel

from app.config import settings

logger = logging.getLogger(__name__)

_model: "WhisperModel | None" = None
_load_lock = Lock()


def _get_model() -> "WhisperModel":
    # Heavy import deferred so the FastAPI app can boot in environments
    # where the model deps haven't been installed yet (e.g. unit tests).
    from faster_whisper import WhisperModel

    global _model
    if _model is not None:
        return _model
    with _load_lock:
        if _model is not None:  # double-checked
            return _model
        logger.info(
            "loading faster-whisper model=%s device=%s compute_type=%s",
            settings.fw_model,
            settings.fw_device,
            settings.fw_compute_type,
        )
        _model = WhisperModel(
            settings.fw_model,
            device=settings.fw_device,
            compute_type=settings.fw_compute_type,
            download_root=str(settings.model_cache_dir / "faster-whisper"),
        )
    return _model


@dataclass(frozen=True)
class Segment:
    start: float
    end: float
    text: str


def transcribe_file(
    path: str | Path,
    *,
    language: str | None = "en",
    beam_size: int = 1,
    vad_filter: bool = True,
) -> tuple[list[Segment], str]:
    """Transcribe an audio file on disk.

    Returns (segments, detected_language).  We default to `language="en"`
    because the YouTube use-case is overwhelmingly English source video,
    and pinning the language saves the 5-second auto-detect step.
    """
    model = _get_model()
    segments_iter, info = model.transcribe(
        str(path),
        language=language,
        beam_size=beam_size,
        vad_filter=vad_filter,
        word_timestamps=False,
    )
    segments = [Segment(start=s.start, end=s.end, text=s.text.strip()) for s in segments_iter]
    return segments, info.language or (language or "")


def warm_up() -> None:
    """Force the model into memory at startup so the first request is fast."""
    _get_model()


def segments_as_dicts(segments: Iterable[Segment]) -> list[dict[str, float | str]]:
    return [{"start": s.start, "end": s.end, "text": s.text} for s in segments]
