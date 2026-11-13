FROM python:3.12-slim

# ffmpeg: merging video+audio and MP3/WAV/FLAC conversion
# deno: JavaScript runtime that current yt-dlp needs for YouTube
RUN apt-get update \
    && apt-get install -y --no-install-recommends ffmpeg ca-certificates \
    && rm -rf /var/lib/apt/lists/*
COPY --from=denoland/deno:bin /deno /usr/local/bin/deno

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY . .

ENV PYTHONUNBUFFERED=1
# ONE worker: job state lives in memory. Threads handle many progress streams.
CMD ["sh", "-c", "gunicorn --workers 1 --threads 32 --timeout 3600 --bind 0.0.0.0:${PORT:-8080} app:app"]
