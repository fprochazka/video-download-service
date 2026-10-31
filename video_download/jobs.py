"""Download jobs: creating one, running it in the background, and failing the ones a restart interrupted"""

import asyncio
import io
import logging
import uuid
from pathlib import Path

from video_download import config, storage
from video_download.errors import classify_error, is_needs_auth
from video_download.storage import ACTIVE_STATUSES, load_metadata, save_metadata, update_metadata
from video_download.ytdlp import (
    build_ydl_opts,
    cleanup_ytdlp_logger,
    collect_downloaded_files,
    extract_video_metadata,
    is_instagram_extractor,
    is_youtube_extractor,
    run_ytdlp,
    setup_ytdlp_logger,
)

logger = logging.getLogger(__name__)


def create_job(url: str, download_url: str, extractor: str) -> str:
    """Create the job directory with a pending metadata.json and return the job's id"""
    download_id = str(uuid.uuid4())
    download_dir = config.DOWNLOADS_DIR / download_id
    download_dir.mkdir(parents=True, exist_ok=True)

    created_at = storage.now_iso()
    metadata = {
        'download_id': download_id,
        'url': url,
        'download_url': download_url,
        'extractor': extractor,
        'status': 'pending',
        'files': [],
        'error': None,
        'error_code': None,
        'error_type': None,
        'created_at': created_at,
        'updated_at': created_at,
    }
    save_metadata(download_dir, metadata)
    return download_id


async def download_youtube(
    download_id: str, download_dir: Path, download_url: str, ytdlp_logger: logging.Logger
) -> dict | None:
    """First attempt without cookies, one retry with the YouTube cookies file when YouTube asks for a login"""
    update_metadata(download_dir, used_youtube_cookies=False)
    ydl_opts = build_ydl_opts(download_dir, ytdlp_logger)
    try:
        return await asyncio.to_thread(run_ytdlp, ydl_opts, download_url)
    except Exception as e:
        cookies_file = config.youtube_cookies_file()
        if cookies_file is None or not is_needs_auth(e):
            raise

    logger.info('Download %s needs a YouTube login, retrying with YouTube cookies from %s', download_id, cookies_file)
    ytdlp_logger.info('YouTube asked for a login, retrying with YouTube cookies from %s', cookies_file)
    update_metadata(download_dir, used_youtube_cookies=True)
    retry_opts = build_ydl_opts(download_dir, ytdlp_logger)
    # A text stream instead of the path, so yt-dlp never writes the cookies back to the file
    retry_opts['cookiefile'] = io.StringIO(cookies_file.read_text(encoding='utf-8'))
    return await asyncio.to_thread(run_ytdlp, retry_opts, download_url)


def mark_failed(download_dir: Path, download_id: str, error: dict, extractor: str | None = None):
    logger.error(
        'Download %s failed: extractor=%s error_code=%s error_type=%s: %s',
        download_id,
        extractor,
        error['error_code'],
        error['error_type'],
        error['error'],
    )
    try:
        update_metadata(download_dir, status='failed', **error)
    except Exception:
        logger.exception('Could not store the failure of download %s', download_id)


async def download_video_task(download_id: str, url: str):
    """Background task to download video"""
    download_dir = config.DOWNLOADS_DIR / download_id
    extractor = None

    try:
        metadata = update_metadata(download_dir, status='downloading')
        extractor = metadata.get('extractor')
        download_url = metadata.get('download_url') or url

        ytdlp_logger = setup_ytdlp_logger(download_id, download_dir)
        if is_youtube_extractor(extractor):
            info = await download_youtube(download_id, download_dir, download_url, ytdlp_logger)
        else:
            ydl_opts = build_ydl_opts(download_dir, ytdlp_logger, instagram_cookies=is_instagram_extractor(extractor))
            info = await asyncio.to_thread(run_ytdlp, ydl_opts, download_url)

        files = collect_downloaded_files(info, download_dir)
        video_metadata = extract_video_metadata(info) if info else {}
        if files:
            update_metadata(download_dir, status='completed', files=files, **video_metadata)
            logger.info('Download %s completed: extractor=%s files=%s', download_id, extractor, files)
        else:
            update_metadata(download_dir, **video_metadata)
            error = {
                'error': 'The download finished without producing any media file.',
                'error_code': 'unknown',
                'error_type': None,
            }
            mark_failed(download_dir, download_id, error, extractor)

    except asyncio.CancelledError as e:
        mark_failed(download_dir, download_id, classify_error(e) | {'error_code': 'interrupted'}, extractor)
        raise
    except Exception as e:
        logger.debug('Download %s raised', download_id, exc_info=True)
        try:
            error = classify_error(e)
        except Exception:
            logger.exception('Could not classify the failure of download %s', download_id)
            error = {'error': str(e) or type(e).__name__, 'error_code': 'unknown', 'error_type': type(e).__name__}
        mark_failed(download_dir, download_id, error, extractor)
    finally:
        cleanup_ytdlp_logger(download_id)


def fail_interrupted_downloads(downloads_dir: Path):
    """Jobs that were running when the service stopped will never finish, mark them failed"""
    for download_dir in downloads_dir.iterdir():
        if not download_dir.is_dir():
            continue
        try:
            metadata = load_metadata(download_dir)
            if not metadata or metadata.get('status') not in ACTIVE_STATUSES:
                continue
            update_metadata(
                download_dir,
                status='failed',
                error='The download was interrupted by a service restart, please try again.',
                error_code='interrupted',
                error_type=None,
            )
            logger.warning('Download %s was interrupted by a restart, marked as failed', download_dir.name)
        except Exception:
            logger.exception('Could not check download %s for interruption', download_dir.name)
