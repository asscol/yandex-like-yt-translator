"""Coqui XTTS-v2 wrapper for Russian voice synthesis (with optional clone).

XTTS-v2 is a multilingual, multi-speaker model that can clone a voice from
a 6-second reference clip.  On CPU it runs at ~5-8x slower than realtime,
so this backend is **proof-of-concept quality** unless the operator gives
it GPU access.  See README for performance notes.

We keep the model singleton-loaded for the process lifetime.
"""

from __future__ import annotations

import logging
import os
from pathlib import Path
from threading import Lock

import numpy as np
import soundfile as sf

from app.config import settings

logger = logging.getLogger(__name__)

# Importing TTS is *expensive* (pulls torch + transformers + a lot more), so
# we defer it until the first real call.  This keeps `pytest` collection
# fast and lets the FastAPI app start before model files are on disk.
_tts = None
_load_lock = Lock()


def _get_tts():
    global _tts
    if _tts is not None:
        return _tts
    with _load_lock:
        if _tts is not None:
            return _tts
        # Make sure caches land on the persistent volume.
        os.environ.setdefault("HF_HOME", str(settings.model_cache_dir / "huggingface"))
        os.environ.setdefault("XDG_CACHE_HOME", str(settings.model_cache_dir / "xdg"))
        os.environ.setdefault("COQUI_TOS_AGREED", settings.coqui_tos_agreed)
        from TTS.api import TTS as CoquiTTS  # noqa: N811  (vendor name)

        logger.info("loading XTTS-v2 model=%s", settings.xtts_model_name)
        gpu = settings.fw_device == "cuda"
        _tts = CoquiTTS(model_name=settings.xtts_model_name, progress_bar=False, gpu=gpu)
    return _tts


def synthesize(
    text: str,
    *,
    out_path: str | Path,
    language: str = "ru",
    speaker_wav: str | Path | None = None,
    speaker: str | None = None,
) -> Path:
    """Synthesize `text` into `out_path` (16-bit PCM WAV at the model's rate).

    Pass `speaker_wav` to clone the voice from a 6+ second reference clip.
    Pass `speaker` to use one of XTTS-v2's built-in pretrained speakers
    (e.g. "Damien Black", "Claribel Dervla").  Exactly one of the two
    should be provided; if neither is given we fall back to a default
    male built-in speaker.
    """
    tts = _get_tts()
    out = Path(out_path)
    out.parent.mkdir(parents=True, exist_ok=True)

    kwargs: dict = {"text": text, "language": language, "file_path": str(out)}
    if speaker_wav is not None:
        kwargs["speaker_wav"] = str(speaker_wav)
    elif speaker is not None:
        kwargs["speaker"] = speaker
    else:
        # XTTS-v2 ships a handful of built-in speakers; pick a male one as a
        # neutral default.  The full list is in tts.speakers.
        kwargs["speaker"] = "Damien Black"

    tts.tts_to_file(**kwargs)
    return out


def synthesize_to_array(
    text: str,
    *,
    language: str = "ru",
    speaker_wav: str | Path | None = None,
    speaker: str | None = None,
) -> tuple[np.ndarray, int]:
    """Same as `synthesize` but returns (samples, sample_rate) without
    writing to disk.  Used by the streaming endpoint."""
    tts = _get_tts()
    kwargs: dict = {"text": text, "language": language}
    if speaker_wav is not None:
        kwargs["speaker_wav"] = str(speaker_wav)
    elif speaker is not None:
        kwargs["speaker"] = speaker
    else:
        kwargs["speaker"] = "Damien Black"
    samples = tts.tts(**kwargs)
    samples = np.asarray(samples, dtype=np.float32)
    sr = int(getattr(tts.synthesizer, "output_sample_rate", 24000))
    return samples, sr


def warm_up() -> None:
    _get_tts()


def write_wav(path: str | Path, samples: np.ndarray, sample_rate: int) -> Path:
    """Helper: dump float32 samples to disk as 16-bit PCM WAV."""
    out = Path(path)
    out.parent.mkdir(parents=True, exist_ok=True)
    samples = np.clip(samples, -1.0, 1.0)
    sf.write(str(out), samples, sample_rate, subtype="PCM_16")
    return out
