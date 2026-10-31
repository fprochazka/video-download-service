"""Garbage collection of large media of old jobs; the job, its metadata, log, subtitles, details and transcripts stay"""

import asyncio
import logging
import stat
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from video_download import transcription
from video_download.config import GcSettings
from video_download.storage import (
    ACTIVE_STATUSES,
    MEDIA,
    artifacts_lock,
    delete_job_files,
    job_artifacts,
    load_metadata,
)

logger = logging.getLogger(__name__)

PARTIAL = 'partial'


@dataclass
class GcSummary:
    files: int = 0
    bytes: int = 0


def is_partial(name: str) -> bool:
    """What yt-dlp leaves behind after an interrupted or failed download"""
    return name.endswith(('.part', '.ytdl')) or '.part-Frag' in name


def parse_time(value) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    # Jobs from before the timestamps carried an offset were written in UTC
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


def job_finished_at(metadata: dict) -> datetime | None:
    """When the job ended; older jobs have no completed_at, their last update is the closest thing to it.

    Never the mtime of a file: yt-dlp used to set it from the server's Last-Modified header.
    """
    for field in ('completed_at', 'updated_at', 'created_at'):
        if (parsed := parse_time(metadata.get(field))) is not None:
            return parsed
    return None


def regular_file_size(path: Path) -> int | None:
    """Size of a regular file that is not a symlink, else None"""
    try:
        st = path.lstat()
    except OSError:
        return None
    return st.st_size if stat.S_ISREG(st.st_mode) else None


def garbage_of_job(download_dir: Path, metadata: dict, now: datetime, settings: GcSettings) -> list[dict]:
    """Files of the job to delete, as {name, kind, size}"""
    finished_at = job_finished_at(metadata)
    if finished_at is None:
        return []
    age = now - finished_at
    old = age > settings.max_age
    failed_partials = metadata.get('status') == 'failed' and age > settings.failed_partials_age

    garbage = []
    if old:
        for entry in job_artifacts(download_dir, metadata):
            if entry.get('kind') != MEDIA:
                continue
            size = regular_file_size(download_dir / entry['name'])
            if size is not None and size > settings.min_size_bytes:
                garbage.append({'name': entry['name'], 'kind': MEDIA, 'size': size})
    for path in sorted(download_dir.iterdir()):
        if not is_partial(path.name):
            continue
        size = regular_file_size(path)
        if size is not None and (failed_partials or (old and size > settings.min_size_bytes)):
            garbage.append({'name': path.name, 'kind': PARTIAL, 'size': size})
    return garbage


def collect_garbage(downloads_dir: Path, now: datetime, settings: GcSettings) -> GcSummary:
    """One pass over all jobs, blocking filesystem work meant for a thread"""
    summary = GcSummary()
    mode = 'would delete' if settings.dry_run else 'deleted'
    for download_dir in sorted(downloads_dir.iterdir()):
        if download_dir.is_symlink() or not download_dir.is_dir():
            continue
        try:
            with artifacts_lock:
                metadata = load_metadata(download_dir)
                # A running transcription reads the media, the next pass gets it
                if not metadata or metadata.get('status') in ACTIVE_STATUSES or transcription.is_active(metadata):
                    continue
                garbage = garbage_of_job(download_dir, metadata, now, settings)
                if not garbage:
                    continue
                if not settings.dry_run:
                    delete_job_files(download_dir, metadata, garbage, reason='gc')
            for entry in garbage:
                logger.info(
                    'GC %s %s/%s (%s, %d bytes)', mode, download_dir.name, entry['name'], entry['kind'], entry['size']
                )
                summary.files += 1
                summary.bytes += entry['size']
        except Exception:
            logger.exception('GC could not clean download %s', download_dir.name)
    logger.info(
        'GC finished%s: %s %d files, %.1f MB',
        ' (dry run)' if settings.dry_run else '',
        mode,
        summary.files,
        summary.bytes / 1024 / 1024,
    )
    return summary


async def run_periodically(downloads_dir: Path, settings: GcSettings):
    """Runs a pass at once and then every interval until cancelled; the filesystem work runs in a thread"""
    while True:
        try:
            await asyncio.to_thread(collect_garbage, downloads_dir, datetime.now(UTC), settings)
        except Exception:
            logger.exception('GC pass failed')
        await asyncio.sleep(settings.interval_seconds)
