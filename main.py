import asyncio
import json
import re
import shutil
import uuid
from pathlib import Path
from typing import List

import yt_dlp
from fastapi import FastAPI, Form, HTTPException
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

app = FastAPI(title="Video Download Service")

# Create downloads directory
DOWNLOADS_DIR = Path("downloads")
DOWNLOADS_DIR.mkdir(exist_ok=True)

# Mount downloads directory for serving files
app.mount("/downloads", StaticFiles(directory=str(DOWNLOADS_DIR)), name="downloads")


class DownloadResult(BaseModel):
    url: str
    download_id: str
    files: List[str]
    metadata: dict | None = None
    error: str | None = None


def make_url_safe_filename(filename: str) -> str:
    """Convert filename to URL-safe format"""
    # Remove or replace problematic characters
    filename = re.sub(r'[^\w\s\-.]', '_', filename)
    # Replace multiple spaces/underscores with single underscore
    filename = re.sub(r'[\s_]+', '_', filename)
    # Remove leading/trailing underscores and dots
    filename = filename.strip('_.')
    return filename


@app.get("/", response_class=HTMLResponse)
async def index():
    """Serve the main page with download form"""
    template_path = Path(__file__).parent / 'templates' / 'index.html'
    with open(template_path, 'r', encoding='utf-8') as f:
        html_content = f.read()
    return HTMLResponse(content=html_content)


@app.post("/download")
async def download_video(url: str = Form(...)):
    """Download video from URL using yt-dlp"""

    # Create a unique download ID
    download_id = str(uuid.uuid4())
    download_dir = DOWNLOADS_DIR / download_id
    download_dir.mkdir(exist_ok=True)

    try:
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
        metadata = None
        info = None
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            loop = asyncio.get_event_loop()
            info = await loop.run_in_executor(
                None,
                ydl.extract_info,
                url,
                False
            )

        # Download the video
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            loop = asyncio.get_event_loop()
            await loop.run_in_executor(
                None,
                ydl.download,
                [url]
            )

        # Save metadata to JSON
        if info:
            metadata = {
                'url': url,
                'download_id': download_id,
                'title': info.get('title'),
                'duration': info.get('duration'),
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

            metadata_file = download_dir / 'metadata.json'
            with open(metadata_file, 'w', encoding='utf-8') as f:
                json.dump(metadata, f, indent=2, ensure_ascii=False)

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

        if not downloaded_files:
            raise HTTPException(status_code=500, detail="No files were downloaded")

        return DownloadResult(
            url=url,
            download_id=download_id,
            files=downloaded_files,
            metadata=metadata if info else None
        )

    except Exception as e:
        # Clean up on error
        import shutil
        shutil.rmtree(download_dir, ignore_errors=True)
        return DownloadResult(url=url, download_id=download_id, files=[], error=str(e))


@app.get("/files")
async def list_files():
    """List all downloads"""
    downloads = []
    for download_dir in DOWNLOADS_DIR.iterdir():
        if download_dir.is_dir():
            metadata_file = download_dir / 'metadata.json'
            if metadata_file.exists():
                with open(metadata_file, 'r', encoding='utf-8') as f:
                    metadata = json.load(f)
                    downloads.append({
                        'download_id': download_dir.name,
                        'title': metadata.get('title'),
                        'url': metadata.get('url')
                    })
    return {"downloads": downloads}


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)
