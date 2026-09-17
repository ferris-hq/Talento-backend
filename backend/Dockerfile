# syntax=docker/dockerfile:1.7
# One image for the API (uvicorn) and the video worker (arq + ffmpeg).

FROM python:3.12-slim AS base
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PROJECT_ENVIRONMENT=/opt/venv
COPY --from=ghcr.io/astral-sh/uv:0.12 /uv /usr/local/bin/uv
WORKDIR /srv

FROM base AS deps
COPY pyproject.toml uv.lock ./
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-dev --no-install-project

FROM base AS model
# MediaPipe pose model, pinned by checksum.
ARG POSE_MODEL_URL=https://storage.googleapis.com/mediapipe-models/pose_landmarker/pose_landmarker_full/float16/latest/pose_landmarker_full.task
ARG POSE_MODEL_SHA256=4eaa5eb7a98365221087693fcc286334cf0858e2eb6e15b506aa4a7ecdcec4ad
RUN python -c "import hashlib,sys,urllib.request; d=urllib.request.urlopen(sys.argv[1], timeout=60).read(); assert hashlib.sha256(d).hexdigest()==sys.argv[2], 'pose model checksum mismatch'; open('/pose_landmarker_full.task','wb').write(d)" "$POSE_MODEL_URL" "$POSE_MODEL_SHA256"

FROM base AS runtime
# libgl1/libglib2.0-0: OpenCV (pulled in by MediaPipe) needs them even without a display.
RUN apt-get update \
 && apt-get install -y --no-install-recommends ffmpeg libgl1 libglib2.0-0 \
 && rm -rf /var/lib/apt/lists/* \
 && groupadd --system app && useradd --system --gid app --home /srv app
COPY --from=deps /opt/venv /opt/venv
COPY app ./app
COPY worker ./worker
COPY --from=model /pose_landmarker_full.task ./models/pose_landmarker_full.task
ENV PATH="/opt/venv/bin:$PATH"
USER app
EXPOSE 8000
HEALTHCHECK --interval=15s --timeout=3s --start-period=10s --retries=3 \
  CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8000/healthz', timeout=2).status == 200 else 1)"
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000", "--workers", "4", "--proxy-headers", "--forwarded-allow-ips", "*"]
