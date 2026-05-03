"""Async job queue for full video translation.

The extension talks to these endpoints:
    POST /jobs/translate-video  -> create a job, returns {id}
    GET  /jobs/{id}             -> poll status / progress
    GET  /jobs/{id}/audio       -> stream the synthesized WAV (only after done)
    DELETE /jobs/{id}           -> cancel / clean up

We deliberately don't use WebSockets — polling once a second is plenty,
and HTTP polling traverses any corp proxy / caching layer cleanly.
"""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path

from fastapi import APIRouter, BackgroundTasks, HTTPException
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field

from app.config import settings
from app.models.jobs import store
from app.services import pipeline

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/jobs", tags=["jobs"])


class TranslateVideoRequest(BaseModel):
    video_url: str = Field(..., description="https://www.youtube.com/watch?v=…")
    target_lang: str = Field("ru", min_length=2, max_length=5)


@router.post("/translate-video")
async def translate_video(req: TranslateVideoRequest, bg: BackgroundTasks):
    job = await store.create(req.video_url, req.target_lang)
    # Run the pipeline as a background task so the HTTP request returns
    # immediately.  FastAPI runs background tasks in the same event loop.
    bg.add_task(pipeline.run, job.id, req.video_url, req.target_lang)
    return {"id": job.id, "status_url": f"/jobs/{job.id}"}


@router.get("/{job_id}")
async def get_job(job_id: str):
    job = await store.get(job_id)
    if job is None:
        raise HTTPException(404, "job not found")
    return job.to_dict()


@router.get("/{job_id}/audio")
async def get_job_audio(job_id: str):
    job = await store.get(job_id)
    if job is None:
        raise HTTPException(404, "job not found")
    if not job.audio_path:
        raise HTTPException(409, f"job is in stage {job.stage.value}; audio not ready")
    p = Path(job.audio_path)
    if not p.exists():
        raise HTTPException(410, "audio expired or was cleaned up")
    return FileResponse(
        p,
        media_type="audio/wav",
        filename=f"{job_id}.wav",
        headers={"Cache-Control": "private, max-age=3600"},
    )


@router.delete("/{job_id}")
async def delete_job(job_id: str):
    job = await store.get(job_id)
    if job is None:
        raise HTTPException(404, "job not found")
    work_dir = settings.job_dir / job_id
    if work_dir.exists():
        for f in work_dir.glob("*"):
            try:
                f.unlink()
            except OSError:
                pass
        try:
            work_dir.rmdir()
        except OSError:
            pass
    return {"ok": True}


# Periodic GC.  Runs every 10 minutes inside the FastAPI lifespan.
async def gc_loop() -> None:
    while True:
        await asyncio.sleep(600)
        try:
            now = asyncio.get_running_loop().time()
            # We can't read created_at via the loop's clock; use store iteration.
            import time

            wall = time.time()
            for jid, job in list(store._jobs.items()):  # noqa: SLF001
                if wall - job.updated_at > settings.job_ttl_seconds:
                    logger.info("GC: removing stale job %s", jid)
                    work_dir = settings.job_dir / jid
                    if work_dir.exists():
                        for f in work_dir.glob("*"):
                            try:
                                f.unlink()
                            except OSError:
                                pass
                        try:
                            work_dir.rmdir()
                        except OSError:
                            pass
                    store._jobs.pop(jid, None)  # noqa: SLF001
        except Exception:  # pragma: no cover
            logger.exception("GC loop error")
