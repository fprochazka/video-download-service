"""The FastAPI app: the web page, job submission, status and file download routes"""

import asyncio
import contextlib
import logging
import uuid
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import Depends, FastAPI, Form, HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse
from pydantic import BaseModel

from video_download import cleanup, config, transcription
from video_download.csrf import page_token, require_csrf, set_token_cookie
from video_download.jobs import create_job, download_video_task, fail_interrupted_downloads
from video_download.storage import ACTIVE_STATUSES, job_artifacts, load_metadata, media_files, remove_artifacts
from video_download.urls import InvalidDownloadUrl, resolve_download_url
from video_download.worker import DOWNLOAD, TRANSCRIPTION, JobWorker

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(name)s - %(levelname)s - %(message)s')
# httpx logs every request URL of the Gemini SDK at INFO, nothing the service log needs
logging.getLogger('httpx').setLevel(logging.WARNING)

TEMPLATES_DIR = Path(__file__).parent / 'templates'
FAVICON_FILE = Path(__file__).parent / 'static' / 'favicon.svg'

logger = logging.getLogger(__name__)


class DownloadInitResponse(BaseModel):
    download_id: str


class Artifact(BaseModel):
    name: str
    kind: str  # media, subtitle, info, transcript
    size: int | None = None
    media: str | None = None  # transcript: the media file it was made from
    lang: str | None = None  # subtitle track key, e.g. en, en-US, cs-orig
    auto: bool | None = None  # subtitle is automatic captions


class DeletedFile(BaseModel):
    name: str
    kind: str
    size: int | None = None
    deleted_at: str
    reason: str  # user, gc


class TranscriptionUsage(BaseModel):
    prompt_tokens: int = 0
    output_tokens: int = 0
    thinking_tokens: int = 0


class TranscriptionWarning(BaseModel):
    code: str
    message: str


class Transcription(BaseModel):
    status: str  # pending, running, completed, failed
    model: str | None = None
    requested_at: str | None = None
    started_at: str | None = None
    completed_at: str | None = None
    error: str | None = None
    error_code: str | None = None
    warnings: list[TranscriptionWarning] = []
    usage: TranscriptionUsage | None = None
    files: list[str] = []


class DownloadStatus(BaseModel):
    download_id: str
    status: str  # pending, downloading, completed, failed
    url: str
    download_url: str | None = None
    extractor: str | None = None
    files: list[str] = []
    artifacts: list[Artifact] = []
    deleted_files: list[DeletedFile] = []
    error: str | None = None
    error_code: str | None = None
    error_type: str | None = None
    used_youtube_cookies: bool | None = None
    subtitles_error: str | None = None
    transcriptions: dict[str, Transcription] = {}
    # Whether the server can transcribe at all (GEMINI_API_KEY is set), the page disables its Transcribe buttons if not
    transcription_available: bool = False
    created_at: str
    updated_at: str
    completed_at: str | None = None
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


@asynccontextmanager
async def lifespan(fastapi_app: FastAPI):
    config.DOWNLOADS_DIR.mkdir(parents=True, exist_ok=True)
    fail_interrupted_downloads(config.DOWNLOADS_DIR)
    transcription.fail_interrupted_transcriptions(config.DOWNLOADS_DIR)

    worker = JobWorker({DOWNLOAD: config.download_concurrency(), TRANSCRIPTION: config.transcription_concurrency()})
    worker.start()
    fastapi_app.state.worker = worker

    gc_settings = config.gc_settings()
    gc_task = None
    if gc_settings.enabled:
        gc_task = asyncio.create_task(cleanup.run_periodically(config.DOWNLOADS_DIR, gc_settings))
    else:
        logger.info('GC is disabled')
    yield
    if gc_task is not None:
        gc_task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await gc_task
    await worker.stop()


app = FastAPI(title='Video Download Service', lifespan=lifespan)


def index_page(request: Request) -> HTMLResponse:
    with open(TEMPLATES_DIR / 'index.html', encoding='utf-8') as f:
        template = f.read()
    token = page_token(request)
    # The token is URL-safe base64, safe inside the HTML attribute as it is
    response = HTMLResponse(content=template.replace('{{ csrf_token }}', token))
    # A cached page would carry a token that no longer matches the cookie
    response.headers['Cache-Control'] = 'no-store'
    set_token_cookie(request, response, token)
    return response


@app.get('/', response_class=HTMLResponse)
async def index(request: Request):
    """Serve the main page with download form"""
    return index_page(request)


@app.get('/favicon.svg', include_in_schema=False)
@app.get('/favicon.ico', include_in_schema=False)
async def favicon():
    # Browsers ask for /favicon.ico on their own, and all current ones render an SVG served from there too
    return FileResponse(FAVICON_FILE, media_type='image/svg+xml', headers={'Cache-Control': 'public, max-age=604800'})


@app.get('/jobs/{download_id}', response_class=HTMLResponse)
async def job_page(request: Request, download_id: uuid.UUID):
    """Serve the page for one job; the page reads the id from the path and polls /status, which 404s an unknown id"""
    return index_page(request)


@app.post('/download', dependencies=[Depends(require_csrf)])
async def download_video(request: Request, url: str = Form(...)):
    """Initiate video download and return UUID immediately"""
    url = url.strip()
    try:
        download_url, extractor = resolve_download_url(url)
    except InvalidDownloadUrl as e:
        raise HTTPException(status_code=400, detail=str(e)) from e

    download_id = create_job(url, download_url, extractor)
    request.app.state.worker.enqueue(DOWNLOAD, download_video_task, download_id, url)

    return DownloadInitResponse(download_id=download_id)


@app.get('/status/{download_id}')
async def get_download_status(download_id: uuid.UUID):
    """Get the status of a download"""
    download_dir = config.DOWNLOADS_DIR / str(download_id)
    metadata = load_metadata(download_dir)
    if not metadata:
        raise HTTPException(status_code=404, detail='Download not found')

    return status_response(download_dir, metadata)


def status_response(download_dir: Path, metadata: dict) -> DownloadStatus:
    return DownloadStatus(
        **(
            metadata
            | {
                'files': media_files(metadata),
                'artifacts': job_artifacts(download_dir, metadata),
                'transcription_available': config.transcription_settings().available,
            }
        )
    )


@app.get('/downloads/{download_id}/{filename}')
async def get_download_file(download_id: uuid.UUID, filename: str):
    """Serve a file of the job, only the artifacts listed in its metadata; metadata.json and download.log stay private"""
    download_dir = config.DOWNLOADS_DIR / str(download_id)
    metadata = load_metadata(download_dir)
    if not metadata or filename not in {a['name'] for a in job_artifacts(download_dir, metadata)}:
        raise HTTPException(status_code=404, detail='File not found')

    path = (download_dir / filename).resolve()
    if path.parent != download_dir.resolve() or not path.is_file():
        raise HTTPException(status_code=404, detail='File not found')

    return FileResponse(path, filename=filename)


@app.delete('/downloads/{download_id}/{filename}', dependencies=[Depends(require_csrf)])
async def delete_download_file(download_id: uuid.UUID, filename: str):
    """Delete one file of a finished job, the job keeps a record of it in deleted_files; returns the job's status"""
    download_dir = config.DOWNLOADS_DIR / str(download_id)
    metadata = load_metadata(download_dir)
    if not metadata:
        raise HTTPException(status_code=404, detail='File not found')
    if metadata.get('status') in ACTIVE_STATUSES:
        raise HTTPException(status_code=409, detail='The download is still running, delete its files after it ends.')
    if filename not in {a['name'] for a in job_artifacts(download_dir, metadata)}:
        raise HTTPException(status_code=404, detail='File not found')
    if (download_dir / filename).resolve().parent != download_dir.resolve():
        raise HTTPException(status_code=404, detail='File not found')
    if transcription.is_active(metadata, filename):
        raise HTTPException(status_code=409, detail='The file is being transcribed, delete it after that ends.')

    metadata = remove_artifacts(download_dir, {filename}, reason='user')
    logger.info('Download %s: deleted %s on user request', download_id, filename)
    return status_response(download_dir, metadata)


@app.post('/downloads/{download_id}/{filename}/transcribe', status_code=202, dependencies=[Depends(require_csrf)])
async def transcribe_file(request: Request, download_id: uuid.UUID, filename: str):
    """Queue a transcription of one media file of a completed job; returns the job's status"""
    download_dir = config.DOWNLOADS_DIR / str(download_id)
    metadata = load_metadata(download_dir)
    if not metadata or filename not in transcription.media_artifact_names(download_dir, metadata):
        raise HTTPException(status_code=404, detail='File not found')
    path = (download_dir / filename).resolve()
    if path.parent != download_dir.resolve() or not path.is_file():
        raise HTTPException(status_code=404, detail='File not found')
    if metadata.get('status') != 'completed':
        raise HTTPException(status_code=409, detail='The download has not completed, transcribe it after it does.')
    settings = config.transcription_settings()
    if not settings.available:
        return JSONResponse(
            status_code=503,
            content={'detail': transcription.ERROR_MESSAGES['not_configured'], 'error_code': 'not_configured'},
        )
    try:
        metadata = transcription.request_transcription(download_dir, filename, settings.model)
    except transcription.TranscriptionConflict:
        raise HTTPException(status_code=409, detail='This file is already being transcribed.') from None

    request.app.state.worker.enqueue(TRANSCRIPTION, transcription.transcribe_media_task, str(download_id), filename)
    logger.info('Download %s: transcription of %s queued', download_id, filename)
    return status_response(download_dir, metadata)
