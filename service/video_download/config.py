"""The service's settings from its environment, and paths and optional files it reads from its working directory.

Every setting is read through a function that parses and checks it. The app checks all of them at startup with
validate(), so a wrong value stops the service with a ConfigError naming the variable instead of failing a job later.
"""

import math
import os
import re
from dataclasses import dataclass, field
from datetime import timedelta
from pathlib import Path
from urllib.parse import urlsplit

DOWNLOADS_DIR = Path(os.environ.get('DOWNLOADS_DIR', 'downloads'))

# Only sent to Instagram jobs, see README
INSTAGRAM_COOKIES_FILE = Path('instagram_cookies.txt')

# Only sent to YouTube on a retry after a needs_auth failure, see README
YOUTUBE_COOKIES_FILE_ENV = 'YOUTUBE_COOKIES_FILE'
YOUTUBE_COOKIES_FILE_DEFAULT = 'youtube_cookies.txt'


class ConfigError(ValueError):
    """A setting of the environment that the service cannot run with; the message never contains a secret"""


def youtube_cookies_file() -> Path | None:
    """The YouTube cookies file, if there is one (a Docker bind mount of a missing file creates a directory)"""
    path = Path(os.environ.get(YOUTUBE_COOKIES_FILE_ENV) or YOUTUBE_COOKIES_FILE_DEFAULT)
    return path if path.is_file() else None


def load_user_config() -> tuple[dict, dict]:
    """Optional extractor_config.py next to the app, see extractor_config.example.py"""
    try:
        from extractor_config import EXTRACTOR_ARGS, YTDLP_OPTS
    except ImportError:
        return {}, {}
    return EXTRACTOR_ARGS, YTDLP_OPTS


# --- Parsing, each value checked


def env_value(name: str) -> str | None:
    """The stripped value of the variable, None when it is unset or empty"""
    value = (os.environ.get(name) or '').strip()
    return value or None


def env_int(name: str, default: int, minimum: int = 1) -> int:
    value = env_value(name)
    if value is None:
        return default
    try:
        number = int(value)
    except ValueError:
        raise ConfigError(f'{name} must be a whole number of at least {minimum}, it is {value!r}.') from None
    if number < minimum:
        raise ConfigError(f'{name} must be a whole number of at least {minimum}, it is {value!r}.')
    return number


def env_float(name: str, default: float, minimum: float = 0, exclusive: bool = False) -> float:
    value = env_value(name)
    if value is None:
        return default
    bound = f'more than {minimum:g}' if exclusive else f'at least {minimum:g}'
    try:
        number = float(value)
    except ValueError:
        raise ConfigError(f'{name} must be a number {bound}, it is {value!r}.') from None
    if not math.isfinite(number) or number < minimum or exclusive and number == minimum:
        raise ConfigError(f'{name} must be a number {bound}, it is {value!r}.')
    return number


TRUE_VALUES = ('1', 'true', 'yes', 'on')
FALSE_VALUES = ('0', 'false', 'no', 'off')


def env_bool(name: str, default: bool) -> bool:
    value = env_value(name)
    if value is None:
        return default
    if value.lower() in TRUE_VALUES:
        return True
    if value.lower() in FALSE_VALUES:
        return False
    raise ConfigError(f'{name} must be true or false, it is {value!r}.')


# --- The settings


def public_base_url() -> str | None:
    """The URL the service is reached at, e.g. https://video-download.example.ts.net, for absolute links to job files"""
    value = env_value('PUBLIC_BASE_URL')
    if value is None:
        return None
    parts = urlsplit(value)
    if parts.scheme not in ('http', 'https') or not parts.hostname or parts.query or parts.fragment:
        raise ConfigError(
            f'PUBLIC_BASE_URL must be an absolute http or https URL such as https://video-download.example.ts.net, '
            f'it is {value!r}.'
        )
    return value.rstrip('/')


# A host name or address, optionally with a port or a * wildcard, as the Host header carries it
HOST_RE = re.compile(r'^[\w.\-\[\]:*]+$')


def mcp_allowed_hosts() -> list[str]:
    """Host headers the MCP endpoint accepts besides localhost: the host of PUBLIC_BASE_URL and MCP_ALLOWED_HOSTS"""
    hosts = [host.strip() for host in (env_value('MCP_ALLOWED_HOSTS') or '').split(',') if host.strip()]
    for host in hosts:
        if not HOST_RE.match(host):
            raise ConfigError(
                f'MCP_ALLOWED_HOSTS must be a comma-separated list of host names, without a scheme or a path; '
                f'{host!r} is not one.'
            )
    base_url = public_base_url()
    if base_url and (hostname := urlsplit(base_url).hostname):
        hosts.insert(0, hostname)
    return hosts


def download_concurrency() -> int:
    """How many downloads run at the same time, the rest wait in the worker's queue"""
    return env_int('DOWNLOAD_CONCURRENCY', 2)


def transcription_concurrency() -> int:
    """How many transcriptions run at the same time"""
    return env_int('TRANSCRIPTION_CONCURRENCY', 2)


DEFAULT_CHUNK_MINUTES = 5
DEFAULT_CHUNK_CONCURRENCY = 4


def chunk_minutes() -> float:
    """The length of a chunk of a long recording that Gemini transcribes, see README"""
    return env_float('TRANSCRIPTION_CHUNK_MINUTES', DEFAULT_CHUNK_MINUTES, minimum=1)


def chunk_concurrency() -> int:
    """How many chunks of one transcription are sent at the same time; TRANSCRIPTION_CONCURRENCY jobs can run at once"""
    return env_int('TRANSCRIPTION_CHUNK_CONCURRENCY', DEFAULT_CHUNK_CONCURRENCY)


DEFAULT_TRANSCRIPTION_MODEL = 'gemini-3.5-flash-lite'
# Any model of the family is accepted, a new one too; one without a known price shows its token counts only
GEMINI_MODEL_PREFIX = 'gemini-'


@dataclass(frozen=True)
class TranscriptionSettings:
    # repr=False keeps the key out of any log line or traceback that prints the settings
    api_key: str | None = field(repr=False)
    model: str

    @property
    def available(self) -> bool:
        return bool(self.api_key)


def transcription_settings() -> TranscriptionSettings:
    """Transcription with Gemini, see README; without GEMINI_API_KEY transcription is off"""
    model = env_value('TRANSCRIPTION_MODEL') or DEFAULT_TRANSCRIPTION_MODEL
    if not model.startswith(GEMINI_MODEL_PREFIX):
        raise ConfigError(f'TRANSCRIPTION_MODEL must be a Gemini model, gemini-..., it is {model!r}.')
    return TranscriptionSettings(api_key=env_value('GEMINI_API_KEY'), model=model)


@dataclass(frozen=True)
class GcSettings:
    enabled: bool
    dry_run: bool
    max_age: timedelta
    min_size_bytes: int
    interval_seconds: float
    # Leftover partial downloads of a failed job are useless, they go regardless of size after this
    failed_partials_age: timedelta = timedelta(days=1)


def gc_settings() -> GcSettings:
    """Garbage collection of large old media, see README"""
    return GcSettings(
        enabled=env_bool('GC_ENABLED', True),
        dry_run=env_bool('GC_DRY_RUN', False),
        max_age=timedelta(days=env_float('GC_MAX_AGE_DAYS', 90)),
        min_size_bytes=int(env_float('GC_MIN_SIZE_MB', 5) * 1024 * 1024),
        interval_seconds=env_float('GC_INTERVAL_HOURS', 6, exclusive=True) * 3600,
    )


def validate():
    """Read every setting once, so a wrong value stops the service at startup; raises ConfigError"""
    from video_download import pricing

    public_base_url()
    mcp_allowed_hosts()
    download_concurrency()
    transcription_concurrency()
    chunk_minutes()
    chunk_concurrency()
    transcription_settings()
    gc_settings()
    pricing.prices()
