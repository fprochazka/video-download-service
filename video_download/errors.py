"""Classification of download failures into the error_code values stored in metadata"""

import asyncio
import logging
import re

from yt_dlp.networking.exceptions import HTTPError, TransportError
from yt_dlp.utils import (
    DownloadCancelled,
    DownloadError,
    GeoRestrictedError,
    PostProcessingError,
    UnsupportedError,
)

logger = logging.getLogger(__name__)

ANSI_ESCAPE_RE = re.compile(r'\x1b\[[0-9;]*[A-Za-z]')

# Checked in order, the first matching group wins
ERROR_MESSAGE_PATTERNS = [
    ('rate_limited', ('rate-limited', 'rate limit', 'try again later', 'too many requests', 'http error 429')),
    (
        'needs_auth',
        (
            'sign in',
            'confirm your age',
            'age-restricted',
            'private video',
            'login',
            'log in',
            'cookies',
            'registered users',
            'inappropriate',
            'unavailable for certain audiences',
        ),
    ),
    ('geo_restricted', ('geo restriction', 'geo-restrict', 'not available in your country', 'from your location')),
    ('unsupported', ('unsupported url',)),
    ('invalid_url', ('is not a valid url',)),
    ('postprocessing', ('postprocessing:', 'ffmpeg')),
    (
        'unavailable',
        (
            'video unavailable',
            'is unavailable',
            'not available',
            'no longer available',
            'has been removed',
            'does not exist',
            'not found',
            'http error 404',
            'http error 410',
        ),
    ),
    ('network', ('timed out', 'connection', 'unable to download webpage', 'name resolution', 'getaddrinfo')),
]


def clean_error_message(message: str) -> str:
    message = ANSI_ESCAPE_RE.sub('', message).strip()
    if message.startswith('ERROR:'):
        message = message[len('ERROR:') :].strip()
    return message


def unwrap_error(exc: BaseException) -> BaseException:
    """yt-dlp raises DownloadError with the original exception in exc_info"""
    if isinstance(exc, DownloadError) and exc.exc_info and exc.exc_info[1] is not None:
        return exc.exc_info[1]
    return exc


def classify_by_type(root: BaseException) -> str | None:
    candidates = [root, getattr(root, 'cause', None)]
    for exc in candidates:
        if exc is None:
            continue
        if isinstance(exc, (DownloadCancelled, KeyboardInterrupt, asyncio.CancelledError)):
            return 'interrupted'
        if isinstance(exc, GeoRestrictedError):
            return 'geo_restricted'
        if isinstance(exc, UnsupportedError):
            return 'unsupported'
        if isinstance(exc, PostProcessingError):
            return 'postprocessing'
        if isinstance(exc, HTTPError):
            if exc.status == 429:
                return 'rate_limited'
            if exc.status == 401:
                return 'needs_auth'
            if exc.status in (404, 410):
                return 'unavailable'
            return 'network'
        if isinstance(exc, TransportError):
            return 'network'
    return None


def classify_by_message(message: str) -> str:
    lowered = message.lower()
    for code, needles in ERROR_MESSAGE_PATTERNS:
        if any(needle in lowered for needle in needles):
            return code
    return 'unknown'


def classify_error(exc: BaseException) -> dict:
    """Turn an exception from a download into the error fields stored in metadata"""
    root = unwrap_error(exc)
    message = clean_error_message(str(exc)) or clean_error_message(str(root)) or type(root).__name__
    code = classify_by_type(root) or classify_by_message(message)
    return {'error': message, 'error_code': code, 'error_type': type(root).__name__}


def is_needs_auth(exc: BaseException) -> bool:
    try:
        return classify_error(exc)['error_code'] == 'needs_auth'
    except Exception:
        logger.exception('Could not classify an error for the YouTube cookies retry')
        return False
