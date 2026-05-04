"""Backend runtime configuration.

All values read from environment variables so we can override them without
rebuilding the image (especially handy on Fly.io's `flyctl secrets set`).
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Literal


def _env_path(name: str, default: str) -> Path:
    """Resolve a path from env, but don't create it eagerly.

    Creating the directory at import time made running unit tests as a
    non-root user fail (`/data` is not writable).  We let the routes /
    services that actually need the directory create it on first use.
    """
    raw = os.environ.get(name, default)
    return Path(raw).expanduser().resolve()


class Settings:
    # Where Whisper / XTTS / yt-dlp store their downloads.  In Fly.io we mount
    # a persistent volume here so model downloads survive restarts.
    model_cache_dir: Path = _env_path("MODEL_CACHE_DIR", "/data/models")
    job_dir: Path = _env_path("JOB_DIR", "/data/jobs")

    # Faster-Whisper model size.  "base" is the cheapest one that still gives
    # usable accuracy on accented English; "small" is noticeably better but
    # ~2x slower on CPU.  Override with FW_MODEL=tiny|base|small|medium.
    fw_model: str = os.environ.get("FW_MODEL", "base")
    # "int8" is significantly faster on CPU than "float32" with negligible
    # quality loss for short YouTube clips.
    fw_compute_type: str = os.environ.get("FW_COMPUTE_TYPE", "int8")
    fw_device: Literal["cpu", "cuda", "auto"] = os.environ.get("FW_DEVICE", "cpu")  # type: ignore[assignment]

    # XTTS configuration
    xtts_model_name: str = os.environ.get(
        "XTTS_MODEL", "tts_models/multilingual/multi-dataset/xtts_v2"
    )
    # Accept Coqui's CPML licence non-interactively (we surface the licence
    # to end users in the README; flipping this env var counts as accepting
    # it for personal/non-commercial use only).
    coqui_tos_agreed: str = os.environ.get("COQUI_TOS_AGREED", "1")

    # Translation provider.  Currently only the free Google Translate gtx
    # endpoint is supported; if that breaks we'll add DeepL/Yandex.
    translate_endpoint: str = "https://translate.googleapis.com/translate_a/single"

    # Job lifetime: how long generated audio files live on disk before we
    # garbage-collect them.  Fly.io shared-cpu volumes are small.
    job_ttl_seconds: int = int(os.environ.get("JOB_TTL_SECONDS", str(60 * 60 * 6)))

    # Hard cap on input video length.  XTTS on CPU is brutal; refuse to even
    # start on hour-long talks.  Override with MAX_VIDEO_SECONDS.
    max_video_seconds: int = int(os.environ.get("MAX_VIDEO_SECONDS", "600"))

    # CORS: which origins may call us.  In production we restrict to the
    # extension's origin (chrome-extension://<id>) plus localhost dev tools.
    cors_origins: list[str] = (
        os.environ.get("CORS_ORIGINS", "*").split(",")
        if os.environ.get("CORS_ORIGINS")
        else ["*"]
    )


settings = Settings()


# Plumb HuggingFace / Coqui caches into the same persistent dir.
os.environ.setdefault("HF_HOME", str(settings.model_cache_dir / "huggingface"))
os.environ.setdefault("XDG_CACHE_HOME", str(settings.model_cache_dir / "xdg"))
os.environ.setdefault("COQUI_TOS_AGREED", settings.coqui_tos_agreed)
