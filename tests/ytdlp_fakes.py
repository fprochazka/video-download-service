"""Test doubles for yt-dlp, shared by conftest and the tests"""

import json
import sys
from pathlib import Path

from yt_dlp.utils import DownloadError, ExtractorError


class FakeYoutubeDL:
    """Stands in for yt_dlp.YoutubeDL.

    `behaviour(opts, url)` runs at extraction, writes the media files and returns the info dict or raises.
    `subtitles_behaviour(params, info)` runs in the subtitles pass (skip_download), writes subtitle files or raises.
    """

    behaviour = None
    subtitles_behaviour = None
    instances = []

    def __init__(self, opts):
        self.opts = opts
        self.params = opts
        self.calls = []
        # Copies of the params of every process_ie_result call
        self.processed = []
        FakeYoutubeDL.instances.append(self)

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def extract_info(self, url, download=True):
        self.calls.append((url, download))
        return FakeYoutubeDL.behaviour(self.opts, url)

    def process_ie_result(self, info, download=True):
        self.processed.append(dict(self.params))
        if self.params.get('skip_download') and FakeYoutubeDL.subtitles_behaviour:
            FakeYoutubeDL.subtitles_behaviour(self.params, info)
        return info

    @staticmethod
    def sanitize_info(info, remove_private_keys=False):
        return json.loads(json.dumps(info, default=str))


def raise_like_ytdlp(exc: Exception):
    """Raise exc the way YoutubeDL.trouble() reports it when ignoreerrors is off"""
    try:
        raise exc
    except Exception as error:
        exc_info = getattr(error, 'exc_info', None)
        if not (exc_info and exc_info[0]):
            exc_info = sys.exc_info()
        raise DownloadError(f'\x1b[0;31mERROR:\x1b[0m {error}', exc_info) from None


def extractor_error_caused_by(cause: Exception, message: str) -> ExtractorError:
    """An ExtractorError raised while handling cause, like InfoExtractor._download_webpage does"""
    try:
        raise cause
    except Exception as err:
        return ExtractorError(message, cause=err)


def write_media(opts: dict, name: str, content: bytes = b'video') -> str:
    path = Path(opts['paths']['home']) / name
    path.write_bytes(content)
    return str(path)


def read_metadata(downloads_dir: Path, download_id: str) -> dict:
    return json.loads((downloads_dir / download_id / 'metadata.json').read_text(encoding='utf-8'))
