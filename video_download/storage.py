"""Job directories on disk and the metadata.json that holds each job's state"""

import json
import os
import tempfile
import threading
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import TextIO

METADATA_FILENAME = 'metadata.json'
LOG_FILENAME = 'download.log'

# Job files that are never offered for download; jobs created before download.log was excluded list it in files
PRIVATE_FILENAMES = frozenset({METADATA_FILENAME, LOG_FILENAME})

# yt-dlp's info dict of the video, offered for download as the job's details
INFO_FILENAME = 'info.json'

ACTIVE_STATUSES = ('pending', 'downloading')

# Kinds of files in a job's artifacts list; files keeps listing only the media names for older clients
MEDIA = 'media'
SUBTITLE = 'subtitle'
INFO = 'info'
# Text made from a media file: <media stem>.transcript.md and .transcript.json, never deleted by the GC
TRANSCRIPT = 'transcript'


# Serialises the read-modify-write of metadata between a user's delete and the garbage collector's thread
artifacts_lock = threading.Lock()


def now_iso() -> str:
    return datetime.now(UTC).isoformat()


def write_atomic(path: Path, write: Callable[[TextIO], object]):
    """Replace the file through a temporary file next to it, so a reader never sees a half-written file"""
    fd, tmp_name = tempfile.mkstemp(dir=path.parent, prefix=f'.{path.stem}.', suffix='.tmp')
    try:
        with os.fdopen(fd, 'w', encoding='utf-8') as f:
            write(f)
            f.flush()
            os.fsync(f.fileno())
        # mkstemp creates the file as 0600, keep it readable on the host like the other job files
        os.chmod(tmp_name, 0o644)
        os.replace(tmp_name, path)
    except BaseException:
        Path(tmp_name).unlink(missing_ok=True)
        raise


def save_metadata(download_dir: Path, metadata: dict):
    """Atomically replace metadata.json"""
    write_atomic(download_dir / METADATA_FILENAME, lambda f: json.dump(metadata, f, indent=2, ensure_ascii=False))


def load_metadata(download_dir: Path) -> dict | None:
    """Load metadata from JSON file"""
    metadata_file = download_dir / METADATA_FILENAME
    if metadata_file.exists():
        with open(metadata_file, encoding='utf-8') as f:
            return json.load(f)
    return None


def update_metadata(download_dir: Path, **fields) -> dict:
    """Merge fields into the stored metadata; created_at is kept, updated_at is always bumped"""
    metadata = load_metadata(download_dir) or {}
    fields.pop('created_at', None)
    metadata.update(fields)
    metadata.setdefault('created_at', now_iso())
    metadata['updated_at'] = now_iso()
    save_metadata(download_dir, metadata)
    return metadata


def media_files(metadata: dict) -> list[str]:
    """The job's media files from metadata, without the private files that old jobs listed"""
    return [name for name in metadata.get('files') or [] if name not in PRIVATE_FILENAMES]


def artifact(download_dir: Path, name: str, kind: str, **extra) -> dict:
    """An entry of the job's artifacts list, sized from the file on disk"""
    path = download_dir / name
    size = path.stat().st_size if path.is_file() else None
    return {'name': name, 'kind': kind, 'size': size, **extra}


def job_artifacts(download_dir: Path, metadata: dict) -> list[dict]:
    """The job's downloadable files; jobs from before artifacts existed have only their media files"""
    if 'artifacts' in metadata:
        return [a for a in metadata['artifacts'] if a.get('name') not in PRIVATE_FILENAMES]
    return [artifact(download_dir, name, MEDIA) for name in media_files(metadata)]


def write_json(path: Path, data: dict):
    with open(path, 'w', encoding='utf-8') as f:
        json.dump(data, f, ensure_ascii=False)


def remove_artifacts(download_dir: Path, names: set[str], reason: str) -> dict:
    """Delete these artifacts of a job from disk and metadata, and record them in deleted_files.

    Names that are not artifacts of the job are ignored; private files are never artifacts. Returns the updated
    metadata.
    """
    with artifacts_lock:
        metadata = load_metadata(download_dir) or {}
        removed = [a for a in job_artifacts(download_dir, metadata) if a['name'] in names]
        return delete_job_files(download_dir, metadata, removed, reason)


def delete_job_files(download_dir: Path, metadata: dict, entries: list[dict], reason: str) -> dict:
    """Unlink the files ({name, kind, size}) of a job, drop them from files and artifacts, record them in deleted_files.

    The caller holds artifacts_lock and passes the metadata it loaded under it. Symlinks are never followed.
    """
    if not entries:
        return metadata
    deleted_at = now_iso()
    for entry in entries:
        path = download_dir / entry['name']
        if path.is_file() and not path.is_symlink():
            path.unlink()
    names = {entry['name'] for entry in entries}
    deleted = [
        {'name': e['name'], 'kind': e['kind'], 'size': e.get('size'), 'deleted_at': deleted_at, 'reason': reason}
        for e in entries
    ]
    return update_metadata(
        download_dir,
        files=[name for name in media_files(metadata) if name not in names],
        artifacts=[a for a in job_artifacts(download_dir, metadata) if a['name'] not in names],
        deleted_files=(metadata.get('deleted_files') or []) + deleted,
    )
