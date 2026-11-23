"""yt-dlp options and the translation of its results into job files and metadata"""

import io
import logging
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

import yt_dlp

from video_download import config, subtitles
from video_download.errors import EXTRACTION, MEDIA, classify_error
from video_download.storage import LOG_FILENAME, PRIVATE_FILENAMES

if TYPE_CHECKING:
    from video_download.progress import DownloadProgress

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
            # yt-dlp would set the file's mtime to the server's Last-Modified, years old for a fresh download
            'updatetime': False,
            # Subtitles are downloaded in a separate pass, see run_ytdlp
            'writesubtitles': False,
            'writeautomaticsub': False,
        }
    )

    # Instagram session cookies (helps with age-restricted content), only for Instagram jobs, other sites get no cookies
    cookies_file = config.INSTAGRAM_COOKIES_FILE
    if instagram_cookies and cookies_file.is_file():
        ydl_opts['cookiefile'] = str(cookies_file)

    return ydl_opts


@dataclass
class DownloadResult:
    info: dict | None
    # Paths yt-dlp reported for the media, before the subtitles pass processes the info again
    media_filepaths: list[str]
    # yt-dlp's info dict made JSON-serialisable, what --write-info-json would write
    clean_info: dict | None = None
    subtitle_tracks: dict[str, bool] | None = None
    subtitles_error: str | None = None
    # The failure of the run; after a failed media download, info and subtitle_tracks are still set
    error: Exception | None = None
    # EXTRACTION or MEDIA, the step that raised error
    error_phase: str | None = None
    # The options of the run, so that fetch_subtitles can fetch the subtitles of a failed run later
    ydl_opts: dict | None = None


def run_ytdlp(ydl_opts: dict, url: str, progress: DownloadProgress | None = None) -> DownloadResult:
    """Extract once, download the media, then the subtitles from the same extraction.

    The subtitles get their own pass because with ignoreerrors off a failed subtitle raises, and yt-dlp writes
    subtitles before the media, so one failed track would fail the whole job. A failed subtitles pass is recorded in
    the result and never raises. With progress, the video details are stored right after the extraction and yt-dlp
    reports its progress to it.

    A failure does not raise, the result has the error and its phase. A failed media download keeps the extracted
    info, but has no subtitles: the caller decides whether to try again or to fetch them with fetch_subtitles.
    """
    if progress is not None:
        ydl_opts = ydl_opts | {
            'progress_hooks': [progress.progress_hook],
            'postprocessor_hooks': [progress.postprocessor_hook],
        }
    with yt_dlp.YoutubeDL(ydl_opts) as ydl:
        try:
            info = ydl.extract_info(url, download=False)
        except Exception as e:
            return DownloadResult(None, [], error=e, error_phase=EXTRACTION, ydl_opts=ydl_opts)
        if info is None:
            return DownloadResult(None, [])
        tracks = subtitles.select_subtitle_tracks(info)
        if progress is not None:
            progress.extracted(info, tracks)
        try:
            downloaded = ydl.process_ie_result(info, download=True)
        except Exception as e:
            clean_info = ydl.sanitize_info(info, remove_private_keys=True)
            return DownloadResult(info, [], clean_info, tracks, error=e, error_phase=MEDIA, ydl_opts=ydl_opts)
        if downloaded is None:
            return DownloadResult(None, [])
        result = DownloadResult(
            downloaded, requested_filepaths(downloaded), ydl.sanitize_info(downloaded, remove_private_keys=True)
        )
        result.subtitle_tracks = tracks
        result.subtitles_error = download_subtitles(ydl, downloaded, tracks, progress)
        return result


def fetch_subtitles(result: DownloadResult, progress: DownloadProgress | None = None):
    """Download the subtitles of a run whose media download failed, from its extraction, without extracting again"""
    if result.info is None or not result.subtitle_tracks or result.ydl_opts is None:
        return
    ydl_opts = dict(result.ydl_opts)
    if isinstance(ydl_opts.get('cookiefile'), io.StringIO):
        # The failed run read the cookies from this stream and wrote them back to it on exit, read them again
        ydl_opts['cookiefile'].seek(0)
    with yt_dlp.YoutubeDL(ydl_opts) as ydl:
        result.subtitles_error = download_subtitles(ydl, result.info, result.subtitle_tracks, progress)


def download_subtitles(ydl, info: dict, tracks: dict[str, bool], progress: DownloadProgress | None) -> str | None:
    """The subtitles pass: write the tracks and nothing else; returns the error of a failed pass, never raises"""
    if not tracks:
        return None
    ydl.params.update(
        {
            'skip_download': True,
            'writesubtitles': True,
            'writeautomaticsub': True,
            'subtitleslangs': subtitles.subtitleslangs_option(tracks),
            'subtitlesformat': 'vtt/best',
        }
    )
    if progress is not None:
        progress.start_subtitles()
    try:
        ydl.process_ie_result(info, download=True)
    except Exception as e:
        error = classify_error(e)['error']
        ydl.params['logger'].warning('Subtitles failed: %s', error)
        return error
    return None


def requested_filepaths(info: dict) -> list[str]:
    """Paths of the media yt-dlp reports as downloaded, including playlist entries (Instagram carousels)"""
    filepaths = []

    def walk(item: dict):
        for requested in item.get('requested_downloads') or []:
            if requested.get('filepath'):
                filepaths.append(requested['filepath'])
        for entry in item.get('entries') or []:
            if entry:
                walk(entry)

    walk(info)
    return filepaths


def collect_downloaded_files(filepaths: list[str], download_dir: Path) -> list[str]:
    """Names of the complete, non-empty media files among the paths yt-dlp reported"""
    root = download_dir.resolve()
    names = []
    for filepath in filepaths:
        path = Path(filepath).resolve()
        if path.parent != root or path.name in PRIVATE_FILENAMES:
            continue
        if path.name.endswith(INCOMPLETE_SUFFIXES):
            continue
        if not path.is_file() or path.stat().st_size == 0:
            continue
        if path.name not in names:
            names.append(path.name)
    return names


def collect_subtitle_files(tracks: dict[str, bool], download_dir: Path) -> list[tuple[str, str]]:
    """(file name, track key) of the subtitle files the subtitles pass wrote, also after it failed halfway"""
    found = []
    for path in sorted(download_dir.iterdir()):
        if path.is_symlink() or not path.is_file() or path.stat().st_size == 0:
            continue
        key = subtitles.subtitle_track_of(path.name, tracks)
        if key is not None:
            found.append((path.name, key))
    return found


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
