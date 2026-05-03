"""FastAPI app entrypoint.

Run locally:
    uv run uvicorn app.main:app --host 0.0.0.0 --port 8000 --reload

Run in Docker / Fly.io: see Dockerfile + fly.toml.
"""

from __future__ import annotations

import asyncio
import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.config import settings
from app.routers import jobs as jobs_router
from app.routers import transcribe, tts

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s :: %(message)s",
)


@asynccontextmanager
async def lifespan(app: FastAPI):
    gc_task = asyncio.create_task(jobs_router.gc_loop())
    try:
        yield
    finally:
        gc_task.cancel()
        try:
            await gc_task
        except (asyncio.CancelledError, BaseException):
            pass


app = FastAPI(
    title="ru-yt-translator backend",
    version="0.1.0",
    lifespan=lifespan,
    description=(
        "Backend for the Russian YouTube Voice Translator extension.  "
        "Provides Whisper transcription, Google Translate proxying, and "
        "XTTS-v2 voice-cloning TTS.  See /docs for the full schema."
    ),
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_origins,
    allow_credentials=False,
    allow_methods=["GET", "POST", "DELETE", "OPTIONS"],
    allow_headers=["*"],
)

app.include_router(tts.router)
app.include_router(transcribe.router)
app.include_router(jobs_router.router)


@app.get("/healthz", tags=["meta"])
async def health() -> dict[str, str]:
    return {"status": "ok", "version": app.version}


@app.get("/", tags=["meta"])
async def root() -> dict[str, str]:
    return {
        "name": "ru-yt-translator-backend",
        "version": app.version,
        "docs": "/docs",
    }
