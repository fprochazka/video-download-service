"""Paths and optional files the service reads from its working directory"""

import os
from dataclasses import dataclass, field
from datetime import timedelta
from pathlib import Path

DOWNLOADS_DIR = Path(os.environ.get('DOWNLOADS_DIR', 'downloads'))

# Only sent to Instagram jobs, see README
INSTAGRAM_COOKIES_FILE = Path('instagram_cookies.txt')

# Only sent to YouTube on a retry after a needs_auth failure, see README
YOUTUBE_COOKIES_FILE_ENV = 'YOUTUBE_COOKIES_FILE'
YOUTUBE_COOKIES_FILE_DEFAULT = 'youtube_cookies.txt'


def youtube_cookies_file() -> Path | None:
    """The YouTube cookies file, if there is one (a Docker bind mount of a missing file creates a directory)"""
    path = Path(os.environ.get(YOUTUBE_COOKIES_FILE_ENV) or YOUTUBE_COOKIES_FILE_DEFAULT)
    return path if path.is_file() else None


def load_user_config() -> tuple[dict, dict]:
    """Optional extractor_config.py next to the app, see extractor_config.example.py"""
    try:
        from extractor_config import EXTRACTOR_ARGS, YTDLP_OPTS
    except ImportError:
        return {}, {}
    return EXTRACTOR_ARGS, YTDLP_OPTS


def env_int(name: str, default: int) -> int:
    value = os.environ.get(name)
    return int(value) if value and value.strip() else default


def download_concurrency() -> int:
    """How many downloads run at the same time, the rest wait in the worker's queue"""
    return env_int('DOWNLOAD_CONCURRENCY', 2)


def transcription_concurrency() -> int:
    """How many transcriptions run at the same time, each one is a request to Gemini"""
    return env_int('TRANSCRIPTION_CONCURRENCY', 2)


DEFAULT_TRANSCRIPTION_MODEL = 'gemini-3.5-flash-lite'


@dataclass(frozen=True)
class TranscriptionSettings:
    # repr=False keeps the key out of any log line or traceback that prints the settings
    api_key: str | None = field(repr=False)
    model: str

    @property
    def available(self) -> bool:
        return bool(self.api_key)


def transcription_settings() -> TranscriptionSettings:
    """Transcription with Gemini, see README; without GEMINI_API_KEY transcription is off"""
    return TranscriptionSettings(
        api_key=(os.environ.get('GEMINI_API_KEY') or '').strip() or None,
        model=(os.environ.get('TRANSCRIPTION_MODEL') or '').strip() or DEFAULT_TRANSCRIPTION_MODEL,
    )


@dataclass(frozen=True)
class GcSettings:
    enabled: bool
    dry_run: bool
    max_age: timedelta
    min_size_bytes: int
    interval_seconds: float
    # Leftover partial downloads of a failed job are useless, they go regardless of size after this
    failed_partials_age: timedelta = timedelta(days=1)


def env_bool(name: str, default: bool) -> bool:
    value = os.environ.get(name)
    if value is None or not value.strip():
        return default
    return value.strip().lower() in ('1', 'true', 'yes', 'on')


def gc_settings() -> GcSettings:
    """Garbage collection of large old media, see README"""
    return GcSettings(
        enabled=env_bool('GC_ENABLED', True),
        dry_run=env_bool('GC_DRY_RUN', False),
        max_age=timedelta(days=float(os.environ.get('GC_MAX_AGE_DAYS') or 90)),
        min_size_bytes=int(float(os.environ.get('GC_MIN_SIZE_MB') or 5) * 1024 * 1024),
        interval_seconds=float(os.environ.get('GC_INTERVAL_HOURS') or 6) * 3600,
    )
