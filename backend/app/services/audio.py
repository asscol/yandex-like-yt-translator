"""Audio helpers: ffmpeg-driven resampling, slicing, and timed mixing.

We shell out to `ffmpeg` rather than depend on a python wrapper because
ffmpeg is already in the Docker image (yt-dlp needs it) and shelling
out keeps memory tiny.
"""

from __future__ import annotations

import asyncio
import logging
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path

import numpy as np
import soundfile as sf

logger = logging.getLogger(__name__)


def _ffmpeg() -> str:
    path = shutil.which("ffmpeg")
    if path is None:
        raise RuntimeError("ffmpeg is required but not found on PATH")
    return path


async def _run(cmd: list[str]) -> None:
    proc = await asyncio.create_subprocess_exec(
        *cmd,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    _, stderr = await proc.communicate()
    if proc.returncode != 0:
        raise RuntimeError(f"{cmd[0]} failed ({proc.returncode}): {stderr.decode(errors='ignore')[-500:]}")


async def slice_audio(
    src: str | Path,
    dst: str | Path,
    *,
    start: float,
    duration: float,
    sample_rate: int = 22050,
    channels: int = 1,
) -> Path:
    """Cut [start, start+duration] seconds out of src and write to dst as WAV."""
    out = Path(dst)
    out.parent.mkdir(parents=True, exist_ok=True)
    cmd = [
        _ffmpeg(),
        "-loglevel", "error", "-y",
        "-ss", f"{start:.3f}",
        "-t", f"{duration:.3f}",
        "-i", str(src),
        "-ac", str(channels),
        "-ar", str(sample_rate),
        "-c:a", "pcm_s16le",
        str(out),
    ]
    await _run(cmd)
    return out


async def to_wav(
    src: str | Path,
    dst: str | Path,
    *,
    sample_rate: int = 16000,
    channels: int = 1,
) -> Path:
    """Convert any audio/video file to 16 kHz mono WAV (Whisper-friendly)."""
    out = Path(dst)
    out.parent.mkdir(parents=True, exist_ok=True)
    cmd = [
        _ffmpeg(),
        "-loglevel", "error", "-y",
        "-i", str(src),
        "-ac", str(channels),
        "-ar", str(sample_rate),
        "-c:a", "pcm_s16le",
        str(out),
    ]
    await _run(cmd)
    return out


@dataclass
class TimedClip:
    start: float            # target start time in seconds
    samples: np.ndarray     # float32 mono
    sample_rate: int


def assemble_timed_track(
    clips: list[TimedClip],
    *,
    total_duration: float,
    sample_rate: int = 24000,
) -> tuple[np.ndarray, int]:
    """Lay out a sequence of TTS clips on a silent track of the requested length.

    If a synthesized clip is **longer** than the slot allotted by the next
    clip's start time we let it spill over (and stop laying down silence
    until it ends) — this avoids cutting off speech mid-word.  If it's
    **shorter**, we leave silence afterwards.

    All clips are resampled to `sample_rate` if needed.
    """
    n = int(total_duration * sample_rate) + 1
    track = np.zeros(n, dtype=np.float32)
    cursor = 0
    for i, c in enumerate(clips):
        samples = c.samples.astype(np.float32, copy=False)
        if c.sample_rate != sample_rate:
            samples = _resample_linear(samples, c.sample_rate, sample_rate)
        target_start = max(int(c.start * sample_rate), cursor)
        end = target_start + samples.shape[0]
        if end > track.shape[0]:
            # extend with zeros if the synthesized track is longer than the
            # source video (rare but possible when XTTS speaks slowly)
            track = np.concatenate([track, np.zeros(end - track.shape[0], dtype=np.float32)])
        track[target_start:end] += samples
        cursor = end
        if i % 10 == 0:
            logger.debug("laid clip %d at %.2fs (len=%.2fs)", i, c.start, samples.shape[0] / sample_rate)
    # Soft-limit any clipping introduced by overlapping tails.
    peak = float(np.max(np.abs(track)) or 1.0)
    if peak > 1.0:
        track /= peak
    return track, sample_rate


def _resample_linear(x: np.ndarray, src_sr: int, dst_sr: int) -> np.ndarray:
    """Cheap linear-interpolation resampler.  Quality is fine for speech;
    avoids pulling scipy/librosa just for this."""
    if src_sr == dst_sr:
        return x
    duration = x.shape[0] / src_sr
    new_n = int(duration * dst_sr)
    src_t = np.linspace(0, duration, num=x.shape[0], endpoint=False)
    dst_t = np.linspace(0, duration, num=new_n, endpoint=False)
    return np.interp(dst_t, src_t, x).astype(np.float32)


def write_wav(path: str | Path, samples: np.ndarray, sample_rate: int) -> Path:
    out = Path(path)
    out.parent.mkdir(parents=True, exist_ok=True)
    sf.write(str(out), np.clip(samples, -1.0, 1.0), sample_rate, subtype="PCM_16")
    return out
