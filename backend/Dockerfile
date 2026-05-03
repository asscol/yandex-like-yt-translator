# Stage 1: build wheels for the heavy ML deps.
FROM python:3.11-slim-bookworm AS builder

ENV PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_NO_CACHE_DIR=1 \
    DEBIAN_FRONTEND=noninteractive

RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential \
    curl \
    && rm -rf /var/lib/apt/lists/*

# Use uv for reproducible, fast resolution.
RUN curl -LsSf https://astral.sh/uv/install.sh | sh \
    && cp /root/.local/bin/uv /usr/local/bin/uv

WORKDIR /build
COPY pyproject.toml ./

RUN uv venv /opt/venv --python 3.11 \
    && . /opt/venv/bin/activate \
    && uv pip install --no-cache --system -r pyproject.toml

# Stage 2: slim runtime image.
FROM python:3.11-slim-bookworm

ENV DEBIAN_FRONTEND=noninteractive \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    COQUI_TOS_AGREED=1 \
    HF_HOME=/data/models/huggingface \
    XDG_CACHE_HOME=/data/models/xdg \
    MODEL_CACHE_DIR=/data/models \
    JOB_DIR=/data/jobs

# ffmpeg is pulled in for yt-dlp / soundfile decoding.
RUN apt-get update && apt-get install -y --no-install-recommends \
    ffmpeg \
    libsndfile1 \
    ca-certificates \
    && rm -rf /var/lib/apt/lists/*

COPY --from=builder /opt/venv /opt/venv
ENV PATH="/opt/venv/bin:${PATH}"

WORKDIR /app
COPY app ./app
COPY pyproject.toml ./

# Fly.io will mount a volume here for model caches + per-job artifacts.
RUN mkdir -p /data/models /data/jobs

EXPOSE 8000

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000", "--workers", "1", "--timeout-keep-alive", "60"]
