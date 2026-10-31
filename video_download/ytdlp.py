"""yt-dlp options and the translation of its results into job files and metadata"""

import logging
from pathlib import Path

import yt_dlp

from video_download import config
from video_download.storage import LOG_FILENAME, METADATA_FILENAME

# Files yt-dlp leaves behind while (or after failing at) downloading, never offered to the user
INCOMPLETE_SUFFIXES = ('.part', '.ytdl', '.temp')

# restrictfilenames keeps the title ASCII without spaces, the id keeps names unique within a job
OUTPUT_TEMPLATE = '%(title).80B-%(id)s.%(ext)s'


def is_youtube_extractor(extractor: str | None) -> bool:
    return bool(extractor) and extractor.startswith('Youtube')


def is_instagram_extractor(extractor: str | None) -> bool:
    return bool(extractor) and extractor.startswith('Instagram')


def build_ydl_opts(download_dir: Path, ytdlp_logger: logging.Logger, instagram_cookies: bool = False) -> dict:
    extractor_args, user_opts = config.load_user_config()

    ydl_opts = {
        'quiet': False,  # Enable output (goes to the logger)
        'no_warnings': False,  # Show warnings in log
        'format': 'bv*+ba/b',
        'merge_output_format': 'mp4',
        'writethumbnail': False,
        'writeinfojson': False,
        'extractor_args': {},
    }
    ydl_opts.update(user_opts)
    ydl_opts['extractor_args'] = {**ydl_opts.get('extractor_args', {}), **extractor_args}

    # Owned by this service, user config must not override them
    ydl_opts.update(
        {
            # Errors have to raise, otherwise a failed job looks like a completed one without files
            'ignoreerrors': False,
            'noplaylist': True,
            'restrictfilenames': True,
            'paths': {'home': str(download_dir)},
            'outtmpl': OUTPUT_TEMPLATE,
            'logger': ytdlp_logger,
        }
    )

    # Instagram session cookies (helps with age-restricted content), only for Instagram jobs, other sites get no cookies
    cookies_file = config.INSTAGRAM_COOKIES_FILE
    if instagram_cookies and cookies_file.is_file():
        ydl_opts['cookiefile'] = str(cookies_file)

    return ydl_opts


def run_ytdlp(ydl_opts: dict, url: str) -> dict | None:
    with yt_dlp.YoutubeDL(ydl_opts) as ydl:
        return ydl.extract_info(url, download=True)


def collect_downloaded_files(info: dict | None, download_dir: Path) -> list[str]:
    """Names of the media files yt-dlp reports as downloaded, including playlist entries (Instagram carousels)"""
    filepaths = []

    def walk(item: dict):
        for requested in item.get('requested_downloads') or []:
            if requested.get('filepath'):
                filepaths.append(requested['filepath'])
        for entry in item.get('entries') or []:
            if entry:
                walk(entry)

    if info:
        walk(info)

    root = download_dir.resolve()
    names = []
    for filepath in filepaths:
        path = Path(filepath).resolve()
        if path.parent != root or path.name in (METADATA_FILENAME, LOG_FILENAME):
            continue
        if path.name.endswith(INCOMPLETE_SUFFIXES):
            continue
        if not path.is_file() or path.stat().st_size == 0:
            continue
        if path.name not in names:
            names.append(path.name)
    return names


def extract_video_metadata(info: dict) -> dict:
    duration = info.get('duration')
    return {
        'title': info.get('title'),
        'duration': int(duration) if duration is not None else None,
        'uploader': info.get('uploader'),
        'upload_date': info.get('upload_date'),
        'description': info.get('description'),
        'ext': info.get('ext'),
        'format': info.get('format'),
        'resolution': info.get('resolution'),
        'thumbnail': info.get('thumbnail'),
        'webpage_url': info.get('webpage_url'),
        'id': info.get('id'),
        'channel': info.get('channel'),
        'view_count': info.get('view_count'),
        'like_count': info.get('like_count'),
    }


def setup_ytdlp_logger(download_id: str, download_dir: Path) -> logging.Logger:
    """yt-dlp output goes to download.log in the job directory instead of stdout"""
    ytdlp_logger = logging.getLogger(f'yt-dlp.{download_id}')
    ytdlp_logger.setLevel(logging.DEBUG)
    handler = logging.FileHandler(download_dir / LOG_FILENAME)
    handler.setFormatter(logging.Formatter('%(asctime)s - %(levelname)s - %(message)s'))
    ytdlp_logger.addHandler(handler)
    # Prevent propagation to root logger
    ytdlp_logger.propagate = False
    return ytdlp_logger


def cleanup_ytdlp_logger(download_id: str):
    """Clean up yt-dlp logger to prevent memory leaks"""
    ytdlp_logger = logging.getLogger(f'yt-dlp.{download_id}')
    for handler in ytdlp_logger.handlers[:]:
        handler.close()
        ytdlp_logger.removeHandler(handler)
