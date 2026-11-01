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
from starlette.routing import Route

from video_download import cleanup, config, download_index, transcription
from video_download.csrf import page_token, require_csrf, set_token_cookie
from video_download.jobs import fail_interrupted_downloads, submit_download
from video_download.mcp_server import create_server
from video_download.mcp_tasks import JobTasksExtension
from video_download.status import status_response
from video_download.storage import ACTIVE_STATUSES, job_artifacts, load_metadata, remove_artifacts
from video_download.urls import InvalidDownloadUrl
from video_download.worker import DOWNLOAD, TRANSCRIPTION, JobWorker

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(name)s - %(levelname)s - %(message)s')
# httpx logs every request URL of the Gemini SDK and the ElevenLabs client at INFO, nothing the service log needs
logging.getLogger('httpx').setLevel(logging.WARNING)

TEMPLATES_DIR = Path(__file__).parent / 'templates'
FAVICON_FILE = Path(__file__).parent / 'static' / 'favicon.svg'

logger = logging.getLogger(__name__)

TRANSCRIPTION_REJECTED_STATUS = {'not_found': 404, 'not_completed': 409, 'conflict': 409}


class DownloadInitResponse(BaseModel):
    download_id: str
    # The job was created earlier for the same video, it runs or has its media; nothing new was queued
    existing: bool = False


@asynccontextmanager
async def lifespan(fastapi_app: FastAPI):
    try:
        config.validate()
    except config.ConfigError as e:
        # One line that names the variable, before uvicorn's traceback of the failed startup
        logger.error('The service cannot start: %s', e)
        raise
    config.DOWNLOADS_DIR.mkdir(parents=True, exist_ok=True)
    fail_interrupted_downloads(config.DOWNLOADS_DIR)
    transcription.fail_interrupted_transcriptions(config.DOWNLOADS_DIR)
    download_index.backfill_index(config.DOWNLOADS_DIR)

    worker = JobWorker({DOWNLOAD: config.download_concurrency(), TRANSCRIPTION: config.transcription_concurrency()})
    worker.start()
    fastapi_app.state.worker = worker

    gc_settings = config.gc_settings()
    gc_task = None
    if gc_settings.enabled:
        gc_task = asyncio.create_task(cleanup.run_periodically(config.DOWNLOADS_DIR, gc_settings))
    else:
        logger.info('GC is disabled')
    mcp_app = create_mcp_app(fastapi_app)
    fastapi_app.state.mcp_app = mcp_app
    async with mcp_app.lifespan(mcp_app):
        yield
    if gc_task is not None:
        gc_task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await gc_task
    await worker.stop()


app = FastAPI(title='Video Download Service', lifespan=lifespan)


def create_mcp_app(fastapi_app: FastAPI):
    """The MCP server's ASGI app; which tools it offers depends on the configuration, read once at startup"""
    server = create_server(lambda: fastapi_app.state.worker, config.transcription_settings())
    server.add_extension(JobTasksExtension())
    # Stateless: each MCP request stands on its own and every tool reads the job from disk, so no session outlives a
    # restart. The Host guard keeps a page on another site from reaching the endpoint through DNS rebinding; there is
    # no auth or CSRF token on /mcp, the service is only reachable on the tailnet
    return server.http_app(
        path='/mcp',
        stateless_http=True,
        host_origin_protection=True,
        allowed_hosts=config.mcp_allowed_hosts(),
    )


class McpEndpoint:
    """/mcp, served by the MCP app the lifespan built; the MCP app routes the request again"""

    async def __call__(self, scope, receive, send):
        await scope['app'].state.mcp_app(scope, receive, send)


# A route and not a mount, so /mcp itself is the endpoint and not a redirect to /mcp/
app.router.routes.append(Route('/mcp', endpoint=McpEndpoint()))


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
async def download_video(request: Request, url: str = Form(...), force: bool = Form(False)):
    """Start the download and return the job's id at once, or the id of the earlier job of the same video"""
    try:
        submission = submit_download(request.app.state.worker, url, force=force)
    except InvalidDownloadUrl as e:
        raise HTTPException(status_code=400, detail=str(e)) from e

    return DownloadInitResponse(download_id=submission.download_id, existing=submission.existing)


@app.get('/status/{download_id}')
async def get_download_status(download_id: uuid.UUID):
    """Get the status of a download"""
    download_dir = config.DOWNLOADS_DIR / str(download_id)
    metadata = load_metadata(download_dir)
    if not metadata:
        raise HTTPException(status_code=404, detail='Download not found')

    return status_response(download_dir, metadata)


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
    try:
        metadata = transcription.submit_transcription(request.app.state.worker, str(download_id), filename)
    except transcription.TranscriptionRejected as e:
        if e.reason == 'not_configured':
            return JSONResponse(status_code=503, content={'detail': e.message, 'error_code': 'not_configured'})
        raise HTTPException(status_code=TRANSCRIPTION_REJECTED_STATUS[e.reason], detail=e.message) from None

    return status_response(config.DOWNLOADS_DIR / str(download_id), metadata)
