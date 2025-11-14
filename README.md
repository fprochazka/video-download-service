# Video Download Service

A simple web service for downloading videos from various platforms using yt-dlp with background processing and status tracking.

## Features

- **Single-page web interface** with clean, modern design
- **Asynchronous downloads** - Submit URL and get UUID immediately, download continues in background
- **Real-time status polling** - Frontend polls every 5 seconds to check download progress
- **UUID-based privacy** - Each download gets a unique UUID, files are not publicly listable
- **Persistent status** - Download ID saved in localStorage, resumes polling on page reload
- **Metadata tracking** - Comprehensive video metadata saved to JSON for each download
- **URL-safe filenames** - Automatic sanitization of filenames for web serving
- **Error handling** - Failed downloads tracked in metadata with error details
- **Download status states**:
  - `pending` - Download queued
  - `downloading` - Video being downloaded
  - `completed` - Ready for download
  - `failed` - Error occurred (with details in metadata)

## Installation

1. Make sure you have UV installed
2. Clone or navigate to this project
3. Install dependencies:

```bash
uv sync
```

## Running the Application

### Using UV (Local Development)

Start the server with:

```bash
uv run python main.py
```

Or use uvicorn directly:

```bash
uv run uvicorn main:app --reload --host 0.0.0.0 --port 8000
```

The application will be available at: http://localhost:8000

### Using Docker

Build the Docker image:

```bash
docker build -t video-download .
```

Run the container:

```bash
docker run -d -p 8000:8000 -v $(pwd)/downloads:/app/downloads --name video-download video-download
```

Or using docker-compose (create a `docker-compose.yml` first):

```bash
docker-compose up -d
```

The application will be available at: http://localhost:8000

**Note:** The `-v $(pwd)/downloads:/app/downloads` flag mounts the downloads directory so your downloads persist outside the container.

## Usage

1. Open http://localhost:8000 in your browser
2. Paste a video URL (e.g., YouTube, Vimeo, etc.)
3. Click "Download"
4. Server returns UUID immediately and starts download in background
5. Page polls status every 5 seconds with spinner
6. When complete, download links appear
7. Click on file links to download videos

### Privacy & Security

- **No public file listing** - Users can only access downloads if they know the UUID
- **UUID-based access control** - Each download directory uses a unique identifier
- **Download ID persistence** - Stored in browser localStorage, survives page reloads

## Directory Structure

```
video-download/
├── main.py              # FastAPI application
├── templates/
│   └── index.html       # Frontend HTML template
├── downloads/           # Download directories (created automatically)
│   └── {uuid}/
│       ├── metadata.json    # Download status and video metadata
│       └── video_file.mp4   # Downloaded video (URL-safe filename)
└── pyproject.toml       # UV project configuration
```

## API Endpoints

### Public Endpoints

- `GET /` - Main web interface
- `POST /download` - Initiate download (returns UUID immediately)
  - Accepts: `url` form parameter
  - Returns: `{"download_id": "uuid"}`
- `GET /status/{download_id}` - Check download status
  - Returns: Status object with download state, files, metadata, errors
- `GET /downloads/{download_id}/{filename}` - Download a specific file

### Metadata Structure

Each download creates a `metadata.json` file with:

```json
{
  "download_id": "uuid",
  "url": "original_url",
  "status": "completed|downloading|pending|failed",
  "files": ["filename.mp4"],
  "error": null,
  "created_at": "2025-01-14T12:00:00",
  "updated_at": "2025-01-14T12:05:00",
  "title": "Video Title",
  "duration": 123,
  "uploader": "Channel Name",
  "upload_date": "20250114",
  "description": "Video description",
  "view_count": 1000,
  "like_count": 50,
  "channel": "Channel Name",
  "thumbnail": "https://...",
  "resolution": "1920x1080"
}
```

## How It Works

1. **Submit** - User submits URL via form
2. **UUID Generation** - Server creates unique download directory
3. **Immediate Response** - UUID returned to client, HTTP request completes
4. **Background Task** - FastAPI BackgroundTasks handles actual download
5. **Status Updates** - Metadata file updated as download progresses
6. **Polling** - Frontend polls `/status/{uuid}` every 5 seconds
7. **Completion** - Files available via `/downloads/{uuid}/{filename}`

## Technologies Used

- **FastAPI** - Web framework with background tasks
- **yt-dlp** - Video download library
- **Uvicorn** - ASGI server
- **HTML/CSS/JavaScript** - Frontend with polling and localStorage
- **Python asyncio** - Async background processing
