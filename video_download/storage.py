"""Job directories on disk and the metadata.json that holds each job's state"""

import json
import os
import tempfile
from datetime import UTC, datetime
from pathlib import Path

METADATA_FILENAME = 'metadata.json'
LOG_FILENAME = 'download.log'

# Job files that are never offered for download; jobs created before download.log was excluded list it in files
PRIVATE_FILENAMES = frozenset({METADATA_FILENAME, LOG_FILENAME})

ACTIVE_STATUSES = ('pending', 'downloading')


def now_iso() -> str:
    return datetime.now(UTC).isoformat()


def save_metadata(download_dir: Path, metadata: dict):
    """Atomically replace metadata.json, so a reader never sees a half-written file"""
    fd, tmp_name = tempfile.mkstemp(dir=download_dir, prefix='.metadata.', suffix='.tmp')
    try:
        with os.fdopen(fd, 'w', encoding='utf-8') as f:
            json.dump(metadata, f, indent=2, ensure_ascii=False)
            f.flush()
            os.fsync(f.fileno())
        # mkstemp creates the file as 0600, keep metadata readable on the host like the other job files
        os.chmod(tmp_name, 0o644)
        os.replace(tmp_name, download_dir / METADATA_FILENAME)
    except BaseException:
        Path(tmp_name).unlink(missing_ok=True)
        raise


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
