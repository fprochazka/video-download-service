# Video Download Service

A simple web service for downloading videos from various platforms using yt-dlp.

## Features

- Single-page web interface with a clean, modern design
- Download videos from YouTube and other supported platforms
- Temporary working directories for each download
- Automatic file serving and download links
- Error handling and user feedback

## Installation

1. Make sure you have UV installed
2. Clone or navigate to this project
3. Install dependencies:

```bash
uv sync
```

## Running the Application

Start the server with:

```bash
uv run python main.py
```

Or use uvicorn directly:

```bash
uv run uvicorn main:app --reload --host 0.0.0.0 --port 8000
```

The application will be available at: http://localhost:8000

## Usage

1. Open http://localhost:8000 in your browser
2. Paste a video URL (e.g., YouTube, Vimeo, etc.)
3. Click "Download"
4. Wait for the download to complete
5. Click on the file links to download the videos to your computer

## Directory Structure

- `main.py` - The FastAPI application
- `downloads/` - Downloaded videos (created automatically)
- `temp/` - Temporary working directories (created automatically)

## API Endpoints

- `GET /` - Main web interface
- `POST /download` - Download a video (accepts `url` form parameter)
- `GET /files` - List all downloaded files
- `GET /downloads/{filename}` - Download a specific file

## Technologies Used

- FastAPI - Web framework
- yt-dlp - Video download library
- Uvicorn - ASGI server
- HTML/CSS/JavaScript - Frontend
