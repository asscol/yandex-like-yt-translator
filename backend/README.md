# ru-yt-translator backend (v0.2 «живые голоса»)

FastAPI service that powers the **«backend»** mode of the Chrome
extension: full-pipeline translation of a YouTube video into a Russian
voice that **clones the original speaker's timbre** (~6 s reference
clip is enough for XTTS-v2).

```
[Chrome extension]
       │
       │  POST /jobs/translate-video
       ▼
[FastAPI]
       │  yt-dlp → wav (16 kHz mono)
       │  Faster-Whisper → [(start,end,text)]
       │  Google Translate (gtx) → [(start,end,ru_text)]
       │  ffmpeg → 6 s ref clip from longest segment
       │  XTTS-v2 (CPU)  → per-segment ru WAV  ← cloned voice
       │  numpy mixer    → laid-out timed track
       ▼
       /jobs/{id}/audio  (~mono WAV, sample_rate from XTTS)
```

## ⚠️ Performance reality

CPU-only XTTS-v2 is **slow**.  Rough numbers on `shared-cpu-2x` (Fly.io
default in `fly.toml`):

| stage           | speed                       |
|-----------------|------------------------------|
| yt-dlp download | network-bound, ~real-time    |
| Faster-Whisper `base` int8 | ~2× real-time         |
| Google Translate batch     | constant, <2 s        |
| XTTS-v2 (cloned voice)     | **~6× slower than RT** |

So a **2-minute** YouTube clip takes ~12 minutes end-to-end.  This is
fine for personal experimentation but you don't want to point a
production app at it.  Switch to GPU (Modal / Replicate / a Fly.io
GPU machine) for ~real-time output.

The hard cap is `MAX_VIDEO_SECONDS=600` (10 min) — the extension will
get a 4xx if the video is longer.

## Local run

```bash
cd backend
uv sync                  # creates .venv, installs deps
uv run uvicorn app.main:app --reload --host 0.0.0.0 --port 8000
```

First request triggers a ~2 GB model download into `MODEL_CACHE_DIR`
(default `/data/models`; set it to e.g. `~/.cache/ru-yt-translator/models`
locally).  Subsequent runs reuse the cache.

OpenAPI / docs UI: http://localhost:8000/docs

## Endpoints

| method  | path                       | description                                   |
|---------|----------------------------|-----------------------------------------------|
| GET     | /healthz                   | liveness probe                                |
| POST    | /tts                       | text → Russian WAV (built-in voice)           |
| POST    | /tts/clone                 | text + reference WAV → cloned-voice WAV       |
| POST    | /transcribe                | audio file → Whisper segments                 |
| POST    | /jobs/translate-video      | start a full pipeline; returns `{id}`         |
| GET     | /jobs/{id}                 | poll status / progress                        |
| GET     | /jobs/{id}/audio           | download synthesized WAV (after `done`)       |
| DELETE  | /jobs/{id}                 | cancel + clean up                             |

The job schema is in `app/models/jobs.py`.  Stage progress and overall
progress are both 0..1; the stage weights live in `STAGE_WEIGHTS`.

## Deploy to Fly.io

```bash
flyctl auth login
flyctl launch --no-deploy --name ru-yt-translator --region fra
flyctl volumes create models --region fra --size 5
flyctl deploy
flyctl scale vm shared-cpu-2x --memory 4096
```

`auto_stop_machines = "stop"` means the VM hibernates when idle — the
first request after a sleep period wakes it up (~10 s cold start, plus
model load on the first translate).  Cost when idle: ~$0.

## Licence notes

- **Faster-Whisper** uses CTranslate2 + the OpenAI Whisper checkpoints
  (MIT for code; CC-BY-NC 4.0 for the SST model weights — the upstream
  Whisper licence).  Fine for personal use.
- **XTTS-v2** is released under the Coqui Public Model Licence (CPML),
  which is **non-commercial only**.  Setting `COQUI_TOS_AGREED=1`
  represents agreement with that licence; if you want to use this in a
  commercial product, swap XTTS for an alternative such as Silero or
  pay Coqui for a commercial licence.
