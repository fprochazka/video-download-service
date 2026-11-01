# Use Python 3.14 slim image
FROM python:3.14-slim

# Set working directory
WORKDIR /app

# Install system dependencies (ffmpeg for merging video/audio)
RUN apt-get update && apt-get install -y --no-install-recommends \
    ffmpeg \
    && rm -rf /var/lib/apt/lists/*

# Install uv
COPY --from=ghcr.io/astral-sh/uv:latest /uv /usr/local/bin/uv

# Copy dependency files
COPY pyproject.toml uv.lock ./

# Install dependencies using uv, compiled to bytecode here because the runtime user cannot write to /app
RUN UV_COMPILE_BYTECODE=1 uv sync --frozen --no-dev

# Copy application files
COPY main.py ./
COPY video_download/ ./video_download/

# Create downloads directory. The compose bind mount hides it; without a mount any runtime uid can write to it.
RUN mkdir -p downloads && chmod 1777 downloads

# Expose port 8000
EXPOSE 8000

# The container runs as a non-root uid that may have no /etc/passwd entry (compose sets it with `user:`),
# so HOME points at a writable directory. Deno (yt-dlp's YouTube challenge solver) and yt-dlp keep their caches under it.
# .venv/bin on PATH lets yt-dlp find the deno binary from the deno package.
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    HOME=/tmp \
    DENO_DIR=/tmp/deno \
    PATH=/app/.venv/bin:$PATH

USER 1000:1000

CMD ["python", "main.py"]
