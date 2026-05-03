"""End-to-end translate-video pipeline.

Long-running.  Owns one Job in JobStore and drives it through the stages.

Stages:
    1. download_audio  (yt-dlp)        -> 16 kHz mono WAV
    2. transcribe      (Faster-Whisper) -> [Segment]
    3. translate       (Google gtx)     -> [russian_text]
    4. extract_voice   (ffmpeg slice)   -> 6-second reference WAV
    5. synthesize      (XTTS-v2 clone)  -> per-segment WAV, then assembled
    6. done                              -> final mixed WAV on disk

Designed to be fault-tolerant in coarse strokes (each stage updates the
job) and aborts cleanly if the JobStore reports the job was cancelled.
"""

from __future__ import annotations

import logging
import time
from pathlib import Path

import numpy as np

from app.config import settings
from app.models.jobs import JobStage, store
from app.services import audio, translate, whisper, xtts, ytdlp

logger = logging.getLogger(__name__)


def _select_reference_segment(segments: list[whisper.Segment]) -> whisper.Segment | None:
    """Pick a segment between 4 and 10 seconds long for voice cloning.

    Shorter than 4s gives XTTS too little to work with; longer than ~12s
    blows past the model's reference cap.  We prefer the longest segment
    in that band so the speaker has more material to imitate.
    """
    candidates = [s for s in segments if 4.0 <= (s.end - s.start) <= 10.0]
    if not candidates:
        # Loosen the band if nothing fits; very short clips still beat using
        # the default speaker.
        candidates = [s for s in segments if 2.0 <= (s.end - s.start) <= 12.0]
    if not candidates:
        return None
    candidates.sort(key=lambda s: (s.end - s.start), reverse=True)
    return candidates[0]


async def run(job_id: str, video_url: str, target_lang: str = "ru") -> None:
    """Drive a single job through the pipeline.  Updates the JobStore as
    it goes.  Never raises — any error sets job.stage = error."""
    t0 = time.monotonic()
    work_dir = settings.job_dir / job_id
    work_dir.mkdir(parents=True, exist_ok=True)

    try:
        # 1. Download audio
        await store.update(job_id, stage=JobStage.downloading, progress=0.1)
        info = await ytdlp.probe(video_url)
        if info.duration > settings.max_video_seconds:
            raise RuntimeError(
                f"video is {info.duration:.0f}s long; max allowed is "
                f"{settings.max_video_seconds}s. Edit MAX_VIDEO_SECONDS to override."
            )
        await store.update(
            job_id,
            stage=JobStage.downloading,
            progress=0.4,
            duration=info.duration,
        )
        wav_in = await ytdlp.download_audio(video_url, work_dir)
        await store.update(job_id, stage=JobStage.downloading, progress=1.0)

        # 2. Transcribe
        await store.update(job_id, stage=JobStage.transcribing, progress=0.1)
        segments, _detected = whisper.transcribe_file(wav_in, language="en")
        if not segments:
            raise RuntimeError("Whisper returned no segments — the video may be silent.")
        await store.update(job_id, stage=JobStage.transcribing, progress=1.0)

        # 3. Translate
        await store.update(job_id, stage=JobStage.translating, progress=0.1)
        ru_texts = await translate.translate_segments([s.text for s in segments], target=target_lang)
        await store.update(job_id, stage=JobStage.translating, progress=1.0)

        # 4. Extract reference clip for voice cloning
        await store.update(job_id, stage=JobStage.extracting_voice, progress=0.1)
        ref_segment = _select_reference_segment(segments)
        ref_clip: Path | None = None
        if ref_segment is not None:
            ref_clip = work_dir / "speaker_ref.wav"
            await audio.slice_audio(
                wav_in,
                ref_clip,
                start=ref_segment.start,
                duration=ref_segment.end - ref_segment.start,
                sample_rate=22050,
                channels=1,
            )
            logger.info(
                "using ref clip %.2f-%.2fs (%.2fs)",
                ref_segment.start, ref_segment.end, ref_segment.end - ref_segment.start,
            )
        else:
            logger.warning("no usable reference clip found; falling back to default speaker")
        await store.update(job_id, stage=JobStage.extracting_voice, progress=1.0)

        # 5. Synthesize each translated segment, lay them out timed
        clips: list[audio.TimedClip] = []
        n = len(ru_texts)
        for i, (seg, ru) in enumerate(zip(segments, ru_texts, strict=True)):
            await store.update(
                job_id,
                stage=JobStage.synthesizing,
                progress=i / max(n, 1),
            )
            if not ru.strip():
                continue
            samples, sr = xtts.synthesize_to_array(
                ru,
                language=target_lang,
                speaker_wav=ref_clip,
            )
            clips.append(audio.TimedClip(start=seg.start, samples=samples, sample_rate=sr))

        if not clips:
            raise RuntimeError("XTTS produced no audio — check the translation step.")

        track, sr = audio.assemble_timed_track(
            clips,
            total_duration=info.duration,
            sample_rate=clips[0].sample_rate,
        )

        out_path = work_dir / "translated.wav"
        audio.write_wav(out_path, track, sr)

        await store.update(
            job_id,
            stage=JobStage.done,
            progress=1.0,
            audio_path=str(out_path),
            sample_rate=sr,
        )
        elapsed = time.monotonic() - t0
        logger.info("job %s finished in %.1fs", job_id, elapsed)

    except Exception as e:
        logger.exception("job %s failed", job_id)
        await store.update(job_id, error=str(e))
