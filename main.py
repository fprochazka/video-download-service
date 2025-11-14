import asyncio
import json
import re
import shutil
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import List

import yt_dlp
from fastapi import BackgroundTasks, FastAPI, Form, HTTPException
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

app = FastAPI(title="Video Download Service")

# Create downloads directory
DOWNLOADS_DIR = Path("downloads")
DOWNLOADS_DIR.mkdir(exist_ok=True)

# Mount downloads directory for serving files
app.mount("/downloads", StaticFiles(directory=str(DOWNLOADS_DIR)), name="downloads")


class DownloadInitResponse(BaseModel):
    download_id: str


class DownloadStatus(BaseModel):
    download_id: str
    status: str  # pending, downloading, completed, failed
    url: str
    files: List[str] = []
    error: str | None = None
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


def make_url_safe_filename(filename: str) -> str:
    """Convert filename to URL-safe format"""
    # Remove or replace problematic characters
    filename = re.sub(r'[^\w\s\-.]', '_', filename)
    # Replace multiple spaces/underscores with single underscore
    filename = re.sub(r'[\s_]+', '_', filename)
    # Remove leading/trailing underscores and dots
    filename = filename.strip('_.')
    return filename


def save_metadata(download_dir: Path, metadata: dict):
    """Save metadata to JSON file"""
    metadata_file = download_dir / 'metadata.json'
    with open(metadata_file, 'w', encoding='utf-8') as f:
        json.dump(metadata, f, indent=2, ensure_ascii=False)


def load_metadata(download_dir: Path) -> dict | None:
    """Load metadata from JSON file"""
    metadata_file = download_dir / 'metadata.json'
    if metadata_file.exists():
        with open(metadata_file, 'r', encoding='utf-8') as f:
            return json.load(f)
    return None


async def download_video_task(download_id: str, url: str):
    """Background task to download video"""
    download_dir = DOWNLOADS_DIR / download_id

    try:
        # Initialize metadata with pending status
        metadata = {
            'download_id': download_id,
            'url': url,
            'status': 'downloading',
            'files': [],
            'error': None,
            'created_at': datetime.now(timezone.utc).isoformat(),
            'updated_at': datetime.now(timezone.utc).isoformat(),
        }
        save_metadata(download_dir, metadata)

        # Configure yt-dlp options
        ydl_opts = {
            'outtmpl': str(download_dir / '%(title)s.%(ext)s'),
            'quiet': True,
            'no_warnings': True,
            'format': 'best',
            'writethumbnail': False,
            'writeinfojson': False,
            # Fix for YouTube JS runtime warning
            'extractor_args': {'youtube': {'player_client': ['android', 'web']}},
        }

        # First, extract info without downloading to get metadata
        info = None
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            loop = asyncio.get_event_loop()
            info = await loop.run_in_executor(
                None,
                ydl.extract_info,
                url,
                False
            )

        # Update metadata with video info
        if info:
            # Convert duration to int if it's a float
            duration = info.get('duration')
            if duration is not None:
                duration = int(duration)

            metadata.update({
                'title': info.get('title'),
                'duration': duration,
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
            })
            metadata['updated_at'] = datetime.now(timezone.utc).isoformat()
            save_metadata(download_dir, metadata)

        # Download the video
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            loop = asyncio.get_event_loop()
            await loop.run_in_executor(
                None,
                ydl.download,
                [url]
            )

        # Get list of downloaded files and rename them to be URL-safe
        downloaded_files = []
        for file_path in download_dir.iterdir():
            if file_path.is_file() and file_path.name != 'metadata.json':
                # Make filename URL-safe
                safe_name = make_url_safe_filename(file_path.name)

                # Rename if needed
                if safe_name != file_path.name:
                    new_path = download_dir / safe_name
                    # Handle conflicts
                    counter = 1
                    while new_path.exists():
                        stem = Path(safe_name).stem
                        suffix = Path(safe_name).suffix
                        new_path = download_dir / f"{stem}_{counter}{suffix}"
                        safe_name = new_path.name
                        counter += 1

                    file_path.rename(new_path)
                    file_path = new_path

                downloaded_files.append(file_path.name)

        # Update metadata with completion status
        metadata['status'] = 'completed'
        metadata['files'] = downloaded_files
        metadata['updated_at'] = datetime.now(timezone.utc).isoformat()
        save_metadata(download_dir, metadata)

    except Exception as e:
        # Update metadata with error status
        metadata = load_metadata(download_dir) or {}
        metadata.update({
            'status': 'failed',
            'error': str(e),
            'updated_at': datetime.now(timezone.utc).isoformat(),
        })
        save_metadata(download_dir, metadata)


@app.get("/", response_class=HTMLResponse)
async def index():
    """Serve the main page with download form"""
    template_path = Path(__file__).parent / 'templates' / 'index.html'
    with open(template_path, 'r', encoding='utf-8') as f:
        html_content = f.read()
    return HTMLResponse(content=html_content)


@app.post("/download")
async def download_video(url: str = Form(...), background_tasks: BackgroundTasks = None):
    """Initiate video download and return UUID immediately"""

    # Create a unique download ID
    download_id = str(uuid.uuid4())
    download_dir = DOWNLOADS_DIR / download_id
    download_dir.mkdir(exist_ok=True)

    # Create initial metadata with pending status
    metadata = {
        'download_id': download_id,
        'url': url,
        'status': 'pending',
        'files': [],
        'error': None,
        'created_at': datetime.now(timezone.utc).isoformat(),
        'updated_at': datetime.now(timezone.utc).isoformat(),
    }
    save_metadata(download_dir, metadata)

    # Add background task to actually download the video
    background_tasks.add_task(download_video_task, download_id, url)

    return DownloadInitResponse(download_id=download_id)


@app.get("/status/{download_id}")
async def get_download_status(download_id: str):
    """Get the status of a download"""
    download_dir = DOWNLOADS_DIR / download_id

    if not download_dir.exists():
        raise HTTPException(status_code=404, detail="Download not found")

    metadata = load_metadata(download_dir)
    if not metadata:
        raise HTTPException(status_code=404, detail="Metadata not found")

    return DownloadStatus(**metadata)


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)
