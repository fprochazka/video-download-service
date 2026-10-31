"""The FastAPI app: the web page, job submission, status and file download routes"""

import logging
import uuid
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import BackgroundTasks, FastAPI, Form, HTTPException
from fastapi.responses import FileResponse, HTMLResponse
from pydantic import BaseModel

from video_download import config
from video_download.jobs import create_job, download_video_task, fail_interrupted_downloads
from video_download.storage import LOG_FILENAME, METADATA_FILENAME, load_metadata
from video_download.urls import InvalidDownloadUrl, resolve_download_url

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(name)s - %(levelname)s - %(message)s')

TEMPLATES_DIR = Path(__file__).parent / 'templates'


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


@asynccontextmanager
async def lifespan(_app: FastAPI):
    config.DOWNLOADS_DIR.mkdir(parents=True, exist_ok=True)
    fail_interrupted_downloads(config.DOWNLOADS_DIR)
    yield


app = FastAPI(title='Video Download Service', lifespan=lifespan)


@app.get('/', response_class=HTMLResponse)
async def index():
    """Serve the main page with download form"""
    template_path = TEMPLATES_DIR / 'index.html'
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

    download_id = create_job(url, download_url, extractor)
    background_tasks.add_task(download_video_task, download_id, url)

    return DownloadInitResponse(download_id=download_id)


@app.get('/status/{download_id}')
async def get_download_status(download_id: uuid.UUID):
    """Get the status of a download"""
    metadata = load_metadata(config.DOWNLOADS_DIR / str(download_id))
    if not metadata:
        raise HTTPException(status_code=404, detail='Download not found')

    return DownloadStatus(**metadata)


@app.get('/downloads/{download_id}/{filename}')
async def get_download_file(download_id: uuid.UUID, filename: str):
    """Serve a downloaded media file, only the ones listed in the job's metadata"""
    download_dir = config.DOWNLOADS_DIR / str(download_id)
    metadata = load_metadata(download_dir)
    if not metadata or filename in (METADATA_FILENAME, LOG_FILENAME) or filename not in (metadata.get('files') or []):
        raise HTTPException(status_code=404, detail='File not found')

    path = (download_dir / filename).resolve()
    if path.parent != download_dir.resolve() or not path.is_file():
        raise HTTPException(status_code=404, detail='File not found')

    return FileResponse(path, filename=filename)
