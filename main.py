import asyncio
import io
import json
import logging
import os
import re
import tempfile
import uuid
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

import yt_dlp
from fastapi import BackgroundTasks, FastAPI, Form, HTTPException
from fastapi.responses import FileResponse, HTMLResponse
from pydantic import BaseModel
from yt_dlp.extractor import gen_extractor_classes
from yt_dlp.extractor.youtube import YoutubeIE
from yt_dlp.networking.exceptions import HTTPError, TransportError
from yt_dlp.utils import (
    DownloadCancelled,
    DownloadError,
    GeoRestrictedError,
    PostProcessingError,
    UnsupportedError,
)

# Configure logging
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(name)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

DOWNLOADS_DIR = Path(os.environ.get('DOWNLOADS_DIR', 'downloads'))
METADATA_FILENAME = 'metadata.json'
LOG_FILENAME = 'download.log'
# Files yt-dlp leaves behind while (or after failing at) downloading, never offered to the user
INCOMPLETE_SUFFIXES = ('.part', '.ytdl', '.temp')

# restrictfilenames keeps the title ASCII without spaces, the id keeps names unique within a job
OUTPUT_TEMPLATE = '%(title).80B-%(id)s.%(ext)s'

PLAYLIST_QUERY_PARAMS = {'list', 'index', 'start_radio', 'pp', 'playnext'}
PLAYLIST_REJECTED_MESSAGE = 'Playlists and channels are not supported, paste a link to a single video.'

ACTIVE_STATUSES = ('pending', 'downloading')

# Only sent to YouTube on a retry after a needs_auth failure, see README
YOUTUBE_COOKIES_FILE_ENV = 'YOUTUBE_COOKIES_FILE'
YOUTUBE_COOKIES_FILE_DEFAULT = 'youtube_cookies.txt'


class DownloadInitResponse(BaseModel):
    download_id: str


class DownloadStatus(BaseModel):
    download_id: str
    status: str  # pending, downloading, completed, failed
    url: str
    download_url: str | None = None
    extractor: str | None = None
    files: list[str] = []
    error: str | None = None
    error_code: str | None = None
    error_type: str | None = None
    used_youtube_cookies: bool | None = None
    created_at: str
    updated_at: str
    # Video metadata fields (optional)
    title: str | None = None
    duration: int | None = None
    uploader: str | None = None
    upload_date: str | None = None
    description: str | None = None
    ext: str | None = None
    format: str | None = None
    resolution: str | None = None
    thumbnail: str | None = None
    webpage_url: str | None = None
    id: str | None = None
    channel: str | None = None
    view_count: int | None = None
    like_count: int | None = None


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


def detect_extractor(url: str) -> type:
    """First yt-dlp extractor that claims the URL (the generic extractor claims everything)"""
    return next(ie for ie in gen_extractor_classes() if ie.suitable(url))


def strip_playlist_params(url: str) -> str:
    parts = urlsplit(url)
    query = parse_qsl(parts.query, keep_blank_values=True)
    kept = [(k, v) for k, v in query if k not in PLAYLIST_QUERY_PARAMS]
    if len(kept) == len(query):
        return url
    return urlunsplit(parts._replace(query=urlencode(kept)))


class InvalidDownloadUrl(ValueError):
    pass


def resolve_download_url(url: str) -> tuple[str, str]:
    """Return (url to pass to yt-dlp, extractor key), or raise InvalidDownloadUrl.

    YouTube links that carry a playlist next to a video (mixes, "play all", music radio) are reduced to the video.
    Pure playlist and channel links are rejected. Other sites are left untouched, an Instagram carousel is a playlist
    too and has to keep working.
    """
    parts = urlsplit(url)
    if parts.scheme not in ('http', 'https') or not parts.netloc:
        raise InvalidDownloadUrl('Enter a valid http(s) URL.')

    extractor = detect_extractor(url)
    if not extractor.ie_key().startswith('Youtube'):
        return url, extractor.ie_key()

    stripped = strip_playlist_params(url)
    if YoutubeIE.suitable(stripped):
        return stripped, YoutubeIE.ie_key()
    stripped_extractor = detect_extractor(stripped)
    if stripped_extractor._RETURN_TYPE == 'video':  # e.g. clips
        return stripped, stripped_extractor.ie_key()
    raise InvalidDownloadUrl(PLAYLIST_REJECTED_MESSAGE)


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


def load_user_config() -> tuple[dict, dict]:
    """Optional extractor_config.py next to the app, see extractor_config.example.py"""
    try:
        from extractor_config import EXTRACTOR_ARGS, YTDLP_OPTS
    except ImportError:
        return {}, {}
    return EXTRACTOR_ARGS, YTDLP_OPTS


def is_youtube_extractor(extractor: str | None) -> bool:
    return bool(extractor) and extractor.startswith('Youtube')


def is_instagram_extractor(extractor: str | None) -> bool:
    return bool(extractor) and extractor.startswith('Instagram')


def youtube_cookies_file() -> Path | None:
    """The YouTube cookies file, if there is one (a Docker bind mount of a missing file creates a directory)"""
    path = Path(os.environ.get(YOUTUBE_COOKIES_FILE_ENV) or YOUTUBE_COOKIES_FILE_DEFAULT)
    return path if path.is_file() else None


def build_ydl_opts(download_dir: Path, ytdlp_logger: logging.Logger, instagram_cookies: bool = False) -> dict:
    extractor_args, user_opts = load_user_config()

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
        }
    )

    # Instagram session cookies (helps with age-restricted content), only for Instagram jobs, other sites get no cookies
    cookies_file = Path('instagram_cookies.txt')
    if instagram_cookies and cookies_file.is_file():
        ydl_opts['cookiefile'] = str(cookies_file)

    return ydl_opts


def run_ytdlp(ydl_opts: dict, url: str) -> dict | None:
    with yt_dlp.YoutubeDL(ydl_opts) as ydl:
        return ydl.extract_info(url, download=True)


def collect_downloaded_files(info: dict | None, download_dir: Path) -> list[str]:
    """Names of the media files yt-dlp reports as downloaded, including playlist entries (Instagram carousels)"""
    filepaths = []

    def walk(item: dict):
        for requested in item.get('requested_downloads') or []:
            if requested.get('filepath'):
                filepaths.append(requested['filepath'])
        for entry in item.get('entries') or []:
            if entry:
                walk(entry)

    if info:
        walk(info)

    root = download_dir.resolve()
    names = []
    for filepath in filepaths:
        path = Path(filepath).resolve()
        if path.parent != root or path.name in (METADATA_FILENAME, LOG_FILENAME):
            continue
        if path.name.endswith(INCOMPLETE_SUFFIXES):
            continue
        if not path.is_file() or path.stat().st_size == 0:
            continue
        if path.name not in names:
            names.append(path.name)
    return names


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


def is_needs_auth(exc: BaseException) -> bool:
    try:
        return classify_error(exc)['error_code'] == 'needs_auth'
    except Exception:
        logger.exception('Could not classify an error for the YouTube cookies retry')
        return False


async def download_youtube(
    download_id: str, download_dir: Path, download_url: str, ytdlp_logger: logging.Logger
) -> dict | None:
    """First attempt without cookies, one retry with the YouTube cookies file when YouTube asks for a login"""
    update_metadata(download_dir, used_youtube_cookies=False)
    ydl_opts = build_ydl_opts(download_dir, ytdlp_logger)
    try:
        return await asyncio.to_thread(run_ytdlp, ydl_opts, download_url)
    except Exception as e:
        cookies_file = youtube_cookies_file()
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
    download_dir = DOWNLOADS_DIR / download_id
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


@asynccontextmanager
async def lifespan(_app: FastAPI):
    DOWNLOADS_DIR.mkdir(parents=True, exist_ok=True)
    fail_interrupted_downloads(DOWNLOADS_DIR)
    yield


app = FastAPI(title='Video Download Service', lifespan=lifespan)


@app.get('/', response_class=HTMLResponse)
async def index():
    """Serve the main page with download form"""
    template_path = Path(__file__).parent / 'templates' / 'index.html'
    with open(template_path, encoding='utf-8') as f:
        html_content = f.read()
    return HTMLResponse(content=html_content)


@app.post('/download')
async def download_video(background_tasks: BackgroundTasks, url: str = Form(...)):
    """Initiate video download and return UUID immediately"""
    url = url.strip()
    try:
        download_url, extractor = resolve_download_url(url)
    except InvalidDownloadUrl as e:
        raise HTTPException(status_code=400, detail=str(e)) from e

    download_id = str(uuid.uuid4())
    download_dir = DOWNLOADS_DIR / download_id
    download_dir.mkdir(parents=True, exist_ok=True)

    created_at = now_iso()
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

    background_tasks.add_task(download_video_task, download_id, url)

    return DownloadInitResponse(download_id=download_id)


@app.get('/status/{download_id}')
async def get_download_status(download_id: uuid.UUID):
    """Get the status of a download"""
    metadata = load_metadata(DOWNLOADS_DIR / str(download_id))
    if not metadata:
        raise HTTPException(status_code=404, detail='Download not found')

    return DownloadStatus(**metadata)


@app.get('/downloads/{download_id}/{filename}')
async def get_download_file(download_id: uuid.UUID, filename: str):
    """Serve a downloaded media file, only the ones listed in the job's metadata"""
    download_dir = DOWNLOADS_DIR / str(download_id)
    metadata = load_metadata(download_dir)
    if not metadata or filename in (METADATA_FILENAME, LOG_FILENAME) or filename not in (metadata.get('files') or []):
        raise HTTPException(status_code=404, detail='File not found')

    path = (download_dir / filename).resolve()
    if path.parent != download_dir.resolve() or not path.is_file():
        raise HTTPException(status_code=404, detail='File not found')

    return FileResponse(path, filename=filename)


if __name__ == '__main__':
    import uvicorn

    uvicorn.run(app, host='0.0.0.0', port=8000)
