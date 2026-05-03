"""yt-dlp wrapper to download just the bestaudio of a YouTube video.

We deliberately don't use the `youtube-dl` library form (it's slower and
needs to import for cold-start) — shelling out matches what most CI
recipes do and is easy to swap with a different backend later.
"""

from __future__ import annotations

import asyncio
import json
import logging
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path

logger = logging.getLogger(__name__)


def _ytdlp() -> str:
    path = shutil.which("yt-dlp")
    if path is None:
        raise RuntimeError("yt-dlp is required but not found on PATH")
    return path


@dataclass(frozen=True)
class VideoInfo:
    id: str
    title: str
    duration: float


async def probe(url: str) -> VideoInfo:
    """Cheap JSON metadata probe — no download."""
    proc = await asyncio.create_subprocess_exec(
        _ytdlp(),
        "--quiet",
        "--no-warnings",
        "--dump-single-json",
        "--no-playlist",
        url,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    stdout, stderr = await proc.communicate()
    if proc.returncode != 0:
        raise RuntimeError(f"yt-dlp probe failed: {stderr.decode(errors='ignore')[-500:]}")
    data = json.loads(stdout.decode())
    return VideoInfo(
        id=data.get("id") or "",
        title=data.get("title") or "",
        duration=float(data.get("duration") or 0.0),
    )


async def download_audio(url: str, out_dir: str | Path) -> Path:
    """Download the best audio-only stream and convert to mono 16 kHz WAV.

    Returns the final WAV path.  yt-dlp + the bundled ffmpeg postprocessor
    do the conversion in a single pass.
    """
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    template = str(out / "%(id)s.%(ext)s")
    cmd = [
        _ytdlp(),
        "--quiet",
        "--no-warnings",
        "--no-playlist",
        "-f", "bestaudio/best",
        "-x",
        "--audio-format", "wav",
        "--audio-quality", "0",
        "--postprocessor-args", "-ar 16000 -ac 1",
        "-o", template,
        url,
    ]
    logger.info("yt-dlp download: %s", url)
    proc = await asyncio.create_subprocess_exec(
        *cmd,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    _, stderr = await proc.communicate()
    if proc.returncode != 0:
        raise RuntimeError(f"yt-dlp failed: {stderr.decode(errors='ignore')[-500:]}")

    # Find the WAV that was written for this download.
    info = await probe(url)
    wav = out / f"{info.id}.wav"
    if not wav.exists():
        # yt-dlp sometimes uses a different id template depending on the URL;
        # fall back to the newest WAV in the directory.
        candidates = sorted(out.glob("*.wav"), key=lambda p: p.stat().st_mtime, reverse=True)
        if not candidates:
            raise RuntimeError("yt-dlp finished but no WAV was produced")
        wav = candidates[0]
    return wav
