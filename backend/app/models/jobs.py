"""Pydantic schemas + in-memory job tracker.

The Fly.io free instance is single-process so a dict is fine for jobs.
For multi-instance scale-out we'd swap this for Redis, but the use-case
here is a personal YouTube translator — single instance is the point.
"""

from __future__ import annotations

import asyncio
import time
import uuid
from dataclasses import dataclass, field
from enum import Enum
from typing import Any


class JobStage(str, Enum):
    queued = "queued"
    downloading = "downloading"
    transcribing = "transcribing"
    translating = "translating"
    extracting_voice = "extracting_voice"
    synthesizing = "synthesizing"
    done = "done"
    error = "error"


@dataclass
class Job:
    id: str
    video_url: str
    target_lang: str = "ru"
    stage: JobStage = JobStage.queued
    progress: float = 0.0  # 0..1 within the current stage
    overall: float = 0.0  # 0..1 across the whole pipeline
    error: str | None = None
    audio_path: str | None = None
    sample_rate: int | None = None
    duration: float | None = None
    created_at: float = field(default_factory=time.time)
    updated_at: float = field(default_factory=time.time)
    extra: dict[str, Any] = field(default_factory=dict)

    def touch(self) -> None:
        self.updated_at = time.time()

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "video_url": self.video_url,
            "target_lang": self.target_lang,
            "stage": self.stage.value,
            "progress": round(self.progress, 4),
            "overall": round(self.overall, 4),
            "error": self.error,
            "audio_url": f"/jobs/{self.id}/audio" if self.audio_path else None,
            "sample_rate": self.sample_rate,
            "duration": self.duration,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "extra": self.extra,
        }


# Stage weights for overall progress (rough; CPU XTTS dominates).
STAGE_WEIGHTS: dict[JobStage, float] = {
    JobStage.queued: 0.0,
    JobStage.downloading: 0.05,
    JobStage.transcribing: 0.15,
    JobStage.translating: 0.05,
    JobStage.extracting_voice: 0.05,
    JobStage.synthesizing: 0.70,
    JobStage.done: 0.0,
    JobStage.error: 0.0,
}


class JobStore:
    def __init__(self) -> None:
        self._jobs: dict[str, Job] = {}
        self._lock = asyncio.Lock()

    async def create(self, video_url: str, target_lang: str = "ru") -> Job:
        async with self._lock:
            jid = uuid.uuid4().hex[:12]
            job = Job(id=jid, video_url=video_url, target_lang=target_lang)
            self._jobs[jid] = job
            return job

    async def get(self, jid: str) -> Job | None:
        return self._jobs.get(jid)

    async def remove(self, jid: str) -> bool:
        async with self._lock:
            return self._jobs.pop(jid, None) is not None

    async def update(
        self,
        jid: str,
        *,
        stage: JobStage | None = None,
        progress: float | None = None,
        error: str | None = None,
        **extra: Any,
    ) -> Job | None:
        async with self._lock:
            job = self._jobs.get(jid)
            if job is None:
                return None
            if stage is not None:
                job.stage = stage
                if progress is None:
                    job.progress = 0.0
            if progress is not None:
                job.progress = max(0.0, min(1.0, progress))
            if error is not None:
                job.error = error
                job.stage = JobStage.error
            for k, v in extra.items():
                if hasattr(job, k):
                    setattr(job, k, v)
                else:
                    job.extra[k] = v
            # recompute overall progress across all preceding stages plus the
            # current stage's fractional progress.
            prior = 0.0
            for s, w in STAGE_WEIGHTS.items():
                if s == job.stage:
                    job.overall = min(1.0, prior + w * job.progress)
                    break
                prior += w
            else:
                job.overall = 1.0 if job.stage == JobStage.done else prior
            if job.stage == JobStage.done:
                job.overall = 1.0
            job.touch()
            return job


store = JobStore()
