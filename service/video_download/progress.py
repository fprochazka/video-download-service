"""The live state of a running download in metadata.json: the video details after extraction, then yt-dlp's progress.

yt-dlp calls the hooks from the download thread many times a second. Every metadata write is an atomic file replace,
so the progress is written at most every WRITE_INTERVAL_SECONDS, and at once when the phase or the part changes.
A hook never raises: a failure to record the progress must not fail the download.
"""

import logging
import math
import os
import threading
import time
from collections.abc import Callable
from pathlib import Path

from video_download import storage
from video_download.ytdlp import extract_video_metadata

logger = logging.getLogger(__name__)

WRITE_INTERVAL_SECONDS = 1.5

DOWNLOADING = 'downloading'
MERGING = 'merging'
POSTPROCESSING = 'postprocessing'
SUBTITLES = 'subtitles'

# yt-dlp's postprocessor that joins the separately downloaded video and audio
MERGER = 'Merger'


def part_kind(info: dict) -> str | None:
    """video or audio for a format that has only one of them, None for a format that has both"""
    has_video = info.get('vcodec') not in (None, 'none')
    has_audio = info.get('acodec') not in (None, 'none')
    if has_video and not has_audio:
        return 'video'
    if has_audio and not has_video:
        return 'audio'
    return None


def number(value) -> float | None:
    if isinstance(value, bool) or not isinstance(value, int | float) or not math.isfinite(value):
        return None
    return float(value)


def integer(value) -> int | None:
    value = number(value)
    return int(value) if value is not None else None


def video_details(info: dict, tracks: dict[str, bool]) -> dict:
    """What the extraction knows before the download: the video metadata, the chosen format and the subtitles to come"""
    requested = info.get('requested_formats') or []
    filesize = info.get('filesize') or info.get('filesize_approx')
    if not filesize and requested:
        filesize = sum(f.get('filesize') or f.get('filesize_approx') or 0 for f in requested) or None
    chapters = info.get('chapters')
    return extract_video_metadata(info) | {
        'filesize_approx': int(filesize) if filesize else None,
        'chapters_count': len(chapters) if isinstance(chapters, list) else None,
        'expected_subtitles': [{'lang': key, 'auto': auto} for key, auto in tracks.items()],
    }


def format_parts(info: dict) -> list[str]:
    """format_id of each format yt-dlp downloads one after another and then merges, empty for a single format"""
    return [f['format_id'] for f in info.get('requested_formats') or [] if f.get('format_id')]


class DownloadProgress:
    """Records a job's video details and download progress; its hooks go to yt-dlp's progress_hooks options"""

    def __init__(self, download_dir: Path, clock: Callable[[], float] = time.monotonic):
        self.download_dir = download_dir
        self.clock = clock
        self.phase = DOWNLOADING
        # format_id of each format yt-dlp downloads separately before merging them, from the extraction
        self.parts: list[str] = []
        self.last_key = None
        self.last_write = None
        self.closed = False
        self.failed = False
        self.lock = threading.Lock()

    def extracted(self, info: dict, tracks: dict[str, bool]):
        """Store the video's details from yt-dlp's extraction, before the media download starts"""
        self.phase = DOWNLOADING
        self.guard(self._extracted, info, tracks)

    def _extracted(self, info: dict, tracks: dict[str, bool]):
        self.parts = format_parts(info)
        self.write(video_details(info, tracks))

    def start_subtitles(self):
        self.phase = SUBTITLES

    def progress_hook(self, status: dict):
        self.guard(self._progress, status)

    def postprocessor_hook(self, status: dict):
        self.guard(self._postprocessor, status)

    def close(self):
        """No hook writes after this, so a thread yt-dlp still runs cannot overwrite the job's final state"""
        with self.lock:
            self.closed = True

    def guard(self, record: Callable, *args):
        try:
            record(*args)
        except Exception:
            # One traceback per job is enough, the hooks keep failing the same way
            if not self.failed:
                self.failed = True
                logger.exception('Could not record the progress of download %s', self.download_dir.name)

    def _progress(self, status: dict):
        if status.get('status') not in ('downloading', 'finished'):
            return
        info = status.get('info_dict') or {}
        downloaded = integer(status.get('downloaded_bytes'))
        total = integer(status.get('total_bytes'))
        estimate = integer(status.get('total_bytes_estimate'))
        total_bytes = total or estimate
        percent = None
        if status['status'] == 'finished':
            percent = 100.0
        elif downloaded is not None and total_bytes:
            percent = round(min(downloaded / total_bytes * 100, 100.0), 1)
        progress = {
            'phase': self.phase,
            'part': None,
            'part_index': None,
            'part_count': None,
            'downloaded_bytes': downloaded,
            'total_bytes': total_bytes,
            'total_estimated': bool(total_bytes) and not total,
            'percent': percent,
            'speed': number(status.get('speed')),
            'eta': integer(status.get('eta')),
            'fragment_index': integer(status.get('fragment_index')),
            'fragment_count': integer(status.get('fragment_count')),
            'filename': os.path.basename(status.get('filename') or '') or None,
        }
        if self.phase == DOWNLOADING and len(self.parts) > 1 and info.get('format_id') in self.parts:
            progress |= {
                'part': part_kind(info),
                'part_index': self.parts.index(info['format_id']) + 1,
                'part_count': len(self.parts),
            }
        self.write_progress(progress, force=status['status'] == 'finished')

    def _postprocessor(self, status: dict):
        if status.get('status') != 'started':
            return
        if self.phase == SUBTITLES:
            return
        phase = MERGING if status.get('postprocessor') == MERGER else POSTPROCESSING
        filename = (status.get('info_dict') or {}).get('filepath')
        self.write_progress({'phase': phase, 'filename': os.path.basename(filename) if filename else None})

    def write_progress(self, progress: dict, force: bool = False):
        key = (progress['phase'], progress.get('part_index'))
        now = self.clock()
        due = self.last_write is None or now - self.last_write >= WRITE_INTERVAL_SECONDS
        if not (force or due or key != self.last_key):
            return
        if self.write({'progress': progress | {'updated_at': storage.now_iso()}}):
            self.last_key = key
            self.last_write = now

    def write(self, fields: dict) -> bool:
        # artifacts_lock keeps a write of the event loop (an MCP task cancel) from being lost between read and replace
        with self.lock, storage.artifacts_lock:
            if self.closed:
                return False
            storage.update_metadata(self.download_dir, **fields)
            return True
