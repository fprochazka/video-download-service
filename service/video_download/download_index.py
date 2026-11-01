"""The index of jobs by video, which finds the earlier job of a link submitted again.

downloads/.index.jsonl has one JSON line per created job: {key, download_id, url, created_at}, where key is the video key
of urls.video_key. The file is only appended to. It only finds candidates: the metadata.json of each job decides whether
that job still counts, so a job whose media was deleted, or that failed, does not stop a new download. Every line can be
rebuilt from the jobs' metadata, which the startup backfill does for any job the index lacks.

The file sits at the root of the downloads directory, outside every job directory, so no route serves it and the GC,
which only looks into job directories, never touches it.
"""

import json
import logging
import os
import threading
from pathlib import Path

from video_download.storage import ACTIVE_STATUSES, load_metadata, media_files
from video_download.urls import InvalidDownloadUrl, normalize_url, resolve_download_url, video_key

logger = logging.getLogger(__name__)

INDEX_FILENAME = '.index.jsonl'

# Held from the lookup of a link to the append of its new job, so two submissions of one link create one job
index_lock = threading.Lock()


def index_path(downloads_dir: Path) -> Path:
    return downloads_dir / INDEX_FILENAME


def read_index(downloads_dir: Path) -> list[dict]:
    """The entries of the index; a line that is not a complete entry, like one a crash cut short, is skipped"""
    path = index_path(downloads_dir)
    try:
        lines = path.read_text(encoding='utf-8').splitlines()
    except FileNotFoundError:
        return []
    entries = []
    for number, line in enumerate(lines, start=1):
        if not line.strip():
            continue
        try:
            entry = json.loads(line)
        except ValueError:
            logger.warning('Skipping line %d of %s, it is not valid JSON', number, path.name)
            continue
        if isinstance(entry, dict) and isinstance(entry.get('key'), str) and isinstance(entry.get('download_id'), str):
            entries.append(entry)
    return entries


def append_entries(downloads_dir: Path, entries: list[dict]):
    """Append the entries and fsync; the caller holds index_lock"""
    if not entries:
        return
    path = index_path(downloads_dir)
    with open(path, 'ab') as f:
        # After a crash in the middle of an append the file ends without a newline, the next entry starts a line of its
        # own instead of joining the cut one
        if f.tell() > 0:
            with open(path, 'rb') as tail:
                tail.seek(-1, os.SEEK_END)
                if tail.read(1) != b'\n':
                    f.write(b'\n')
        f.write(b''.join(json.dumps(entry, ensure_ascii=False).encode('utf-8') + b'\n' for entry in entries))
        f.flush()
        os.fsync(f.fileno())


def index_entry(key: str, metadata: dict) -> dict:
    return {
        'key': key,
        'download_id': metadata['download_id'],
        'url': metadata.get('url'),
        'created_at': metadata.get('created_at'),
    }


def has_media(download_dir: Path, metadata: dict) -> bool:
    """At least one media file of the job is still on disk, not deleted by the user or the GC"""
    for name in media_files(metadata):
        path = download_dir / name
        if path.is_file() and not path.is_symlink():
            return True
    return False


def find_existing_job(downloads_dir: Path, key: str) -> dict | None:
    """The metadata of the newest job of the key that is pending or downloading, or completed with its media on disk"""
    candidates = [entry for entry in read_index(downloads_dir) if entry['key'] == key]
    candidates.sort(key=lambda entry: entry.get('created_at') or '', reverse=True)
    for entry in candidates:
        download_dir = downloads_dir / entry['download_id']
        if download_dir.is_symlink() or not download_dir.is_dir():
            continue
        try:
            metadata = load_metadata(download_dir)
        except Exception:
            logger.exception('Could not read job %s of the index', entry['download_id'])
            continue
        if not metadata:
            continue
        status = metadata.get('status')
        if status in ACTIVE_STATUSES or (status == 'completed' and has_media(download_dir, metadata)):
            return metadata
    return None


def job_key(metadata: dict) -> str:
    """The video key of a stored job; jobs from before the extractor was stored get it from their URL"""
    download_url = metadata.get('download_url')
    extractor = metadata.get('extractor')
    if download_url and extractor:
        return video_key(download_url, extractor)
    url = (metadata.get('url') or '').strip()
    try:
        return video_key(*resolve_download_url(url))
    except InvalidDownloadUrl:
        return 'url:' + normalize_url(url)


def backfill_index(downloads_dir: Path) -> int:
    """Append an entry for every job the index lacks, return how many; a run on a complete index only lists the jobs"""
    with index_lock:
        indexed = {entry['download_id'] for entry in read_index(downloads_dir)}
        missing = []
        for download_dir in sorted(downloads_dir.iterdir()):
            if download_dir.name in indexed or download_dir.is_symlink() or not download_dir.is_dir():
                continue
            try:
                metadata = load_metadata(download_dir)
                if not metadata or not metadata.get('url'):
                    continue
                metadata = metadata | {'download_id': download_dir.name}
                missing.append(index_entry(job_key(metadata), metadata))
            except Exception:
                logger.exception('Could not index job %s', download_dir.name)
        append_entries(downloads_dir, missing)
    if missing:
        logger.info('Added %d jobs to the download index', len(missing))
    return len(missing)
