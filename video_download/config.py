"""Paths and optional files the service reads from its working directory"""

import os
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
