import asyncio
import hashlib
import os
import shutil
from pathlib import Path
from typing import List

import yt_dlp
from fastapi import FastAPI, Form, HTTPException
from fastapi.responses import HTMLResponse, FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

app = FastAPI(title="Video Download Service")

# Create directories for downloads and temp work
DOWNLOADS_DIR = Path("downloads")
TEMP_DIR = Path("temp")
DOWNLOADS_DIR.mkdir(exist_ok=True)
TEMP_DIR.mkdir(exist_ok=True)

# Mount downloads directory for serving files
app.mount("/downloads", StaticFiles(directory=str(DOWNLOADS_DIR)), name="downloads")


class DownloadResult(BaseModel):
    url: str
    files: List[str]
    error: str | None = None


@app.get("/", response_class=HTMLResponse)
async def index():
    """Serve the main page with download form"""
    html_content = """
    <!DOCTYPE html>
    <html>
    <head>
        <title>Video Download Service</title>
        <style>
            body {
                font-family: Arial, sans-serif;
                max-width: 800px;
                margin: 50px auto;
                padding: 20px;
                background-color: #f5f5f5;
            }
            .container {
                background-color: white;
                padding: 30px;
                border-radius: 8px;
                box-shadow: 0 2px 4px rgba(0,0,0,0.1);
            }
            h1 {
                color: #333;
                margin-bottom: 30px;
            }
            .form-group {
                margin-bottom: 20px;
            }
            label {
                display: block;
                margin-bottom: 8px;
                font-weight: bold;
                color: #555;
            }
            input[type="url"] {
                width: 100%;
                padding: 10px;
                border: 1px solid #ddd;
                border-radius: 4px;
                font-size: 14px;
                box-sizing: border-box;
            }
            button {
                background-color: #007bff;
                color: white;
                padding: 12px 30px;
                border: none;
                border-radius: 4px;
                font-size: 16px;
                cursor: pointer;
                transition: background-color 0.3s;
            }
            button:hover {
                background-color: #0056b3;
            }
            button:disabled {
                background-color: #ccc;
                cursor: not-allowed;
            }
            .status {
                margin-top: 20px;
                padding: 15px;
                border-radius: 4px;
                display: none;
            }
            .status.loading {
                display: block;
                background-color: #fff3cd;
                color: #856404;
                border: 1px solid #ffeaa7;
            }
            .status.success {
                display: block;
                background-color: #d4edda;
                color: #155724;
                border: 1px solid #c3e6cb;
            }
            .status.error {
                display: block;
                background-color: #f8d7da;
                color: #721c24;
                border: 1px solid #f5c6cb;
            }
            .files-list {
                list-style: none;
                padding: 0;
                margin-top: 15px;
            }
            .files-list li {
                padding: 10px;
                margin-bottom: 8px;
                background-color: #f8f9fa;
                border-radius: 4px;
                border-left: 3px solid #007bff;
            }
            .files-list a {
                color: #007bff;
                text-decoration: none;
                font-weight: 500;
            }
            .files-list a:hover {
                text-decoration: underline;
            }
            .spinner {
                display: inline-block;
                width: 14px;
                height: 14px;
                border: 2px solid #856404;
                border-radius: 50%;
                border-top-color: transparent;
                animation: spin 1s linear infinite;
                margin-right: 8px;
            }
            @keyframes spin {
                to { transform: rotate(360deg); }
            }
        </style>
    </head>
    <body>
        <div class="container">
            <h1>Video Download Service</h1>
            <form id="downloadForm">
                <div class="form-group">
                    <label for="url">Video URL:</label>
                    <input type="url" id="url" name="url" required
                           placeholder="https://www.youtube.com/watch?v=..." />
                </div>
                <button type="submit" id="submitBtn">Download</button>
            </form>

            <div id="status" class="status"></div>
        </div>

        <script>
            const form = document.getElementById('downloadForm');
            const statusDiv = document.getElementById('status');
            const submitBtn = document.getElementById('submitBtn');
            const urlInput = document.getElementById('url');

            form.addEventListener('submit', async (e) => {
                e.preventDefault();

                const url = urlInput.value.trim();
                if (!url) return;

                // Show loading state
                statusDiv.className = 'status loading';
                statusDiv.innerHTML = '<div><span class="spinner"></span>Downloading video, please wait...</div>';
                submitBtn.disabled = true;

                try {
                    const formData = new FormData();
                    formData.append('url', url);

                    const response = await fetch('/download', {
                        method: 'POST',
                        body: formData
                    });

                    const result = await response.json();

                    if (response.ok && result.files && result.files.length > 0) {
                        // Show success with file list
                        statusDiv.className = 'status success';
                        let filesHtml = '<strong>Download completed!</strong>';
                        filesHtml += '<ul class="files-list">';
                        result.files.forEach(file => {
                            filesHtml += `<li><a href="/downloads/${file}" download>${file}</a></li>`;
                        });
                        filesHtml += '</ul>';
                        statusDiv.innerHTML = filesHtml;
                    } else {
                        // Show error
                        statusDiv.className = 'status error';
                        statusDiv.innerHTML = `<strong>Error:</strong> ${result.error || 'Download failed'}`;
                    }
                } catch (error) {
                    statusDiv.className = 'status error';
                    statusDiv.innerHTML = `<strong>Error:</strong> ${error.message}`;
                } finally {
                    submitBtn.disabled = false;
                }
            });
        </script>
    </body>
    </html>
    """
    return HTMLResponse(content=html_content)


@app.post("/download")
async def download_video(url: str = Form(...)):
    """Download video from URL using yt-dlp"""

    # Create a unique working directory for this download
    url_hash = hashlib.md5(url.encode()).hexdigest()[:8]
    work_dir = TEMP_DIR / url_hash
    work_dir.mkdir(exist_ok=True)

    try:
        # Configure yt-dlp options
        ydl_opts = {
            'outtmpl': str(work_dir / '%(title)s.%(ext)s'),
            'quiet': False,
            'no_warnings': False,
            'format': 'best',
        }

        # Download the video
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            await asyncio.get_event_loop().run_in_executor(
                None,
                lambda: ydl.download([url])
            )

        # Get list of downloaded files
        downloaded_files = []
        for file_path in work_dir.iterdir():
            if file_path.is_file():
                # Move file to downloads directory
                dest_path = DOWNLOADS_DIR / file_path.name

                # Handle duplicate filenames
                counter = 1
                original_stem = file_path.stem
                original_suffix = file_path.suffix
                while dest_path.exists():
                    dest_path = DOWNLOADS_DIR / f"{original_stem}_{counter}{original_suffix}"
                    counter += 1

                shutil.move(str(file_path), str(dest_path))
                downloaded_files.append(dest_path.name)

        # Clean up temp directory
        shutil.rmtree(work_dir, ignore_errors=True)

        if not downloaded_files:
            raise HTTPException(status_code=500, detail="No files were downloaded")

        return DownloadResult(url=url, files=downloaded_files)

    except Exception as e:
        # Clean up on error
        shutil.rmtree(work_dir, ignore_errors=True)
        return DownloadResult(url=url, files=[], error=str(e))


@app.get("/files")
async def list_files():
    """List all downloaded files"""
    files = [f.name for f in DOWNLOADS_DIR.iterdir() if f.is_file()]
    return {"files": files}


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)
