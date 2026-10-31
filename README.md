# Video Download Service

A simple web service for downloading videos from various platforms using yt-dlp with background processing and status tracking.

## Features

- **Single-page web interface** with clean, modern design
- **Asynchronous downloads** - Submit URL and get UUID immediately, download continues in background
- **Real-time status polling** - Frontend polls every 5 seconds to check download progress
- **UUID-based privacy** - Each download gets a unique UUID, files are not publicly listable
- **Shareable job page** - Each download has its own page at `/jobs/{download_id}`, a reload or a shared link shows the same job
- **Metadata tracking** - Comprehensive video metadata saved to JSON for each download
- **URL-safe filenames** - yt-dlp's `restrictfilenames` keeps names ASCII without spaces, the video id keeps them unique
- **Best quality** - Downloads the best video and audio streams and merges them into mp4 with ffmpeg
- **Subtitles and details** - Each job also downloads the subtitles in the video's own language and in English, and the video details yt-dlp extracted (`info.json`). A manual subtitle track wins over automatic captions of the same language; of the automatic captions only the speech recognition track (YouTube's `<lang>-orig`) and the English one are taken, never the ~100 machine translations. A failed subtitle download does not fail the job, the error is in `subtitles_error`
- **File list with delete** - The job page lists the files grouped as media, subtitles and details, with their size, a download link and a delete button. Deleted files stay listed as deleted, with the date
- **Single videos only** - A YouTube link to a video inside a playlist or mix downloads just that video; playlist and channel links are rejected. Other sites are untouched, so Instagram carousels still download all their items
- **Error handling** - Failed downloads are tracked in metadata with the error message and a machine-readable `error_code`, and logged to the service log
- **Download status states**:
  - `pending` - Download queued
  - `downloading` - Video being downloaded
  - `completed` - At least one media file was downloaded
  - `failed` - Error occurred, or yt-dlp finished without a media file (details in `error`, `error_code`, `error_type`)
- **Cleanup of old media** - Media files over 5 MB are deleted 90 days after their job finished, the job page and its subtitles and details stay, see [Garbage collection](#garbage-collection)
- **Restart safety** - Downloads that were running when the service stopped are marked `failed` with `error_code` `interrupted` on the next start

## Installation

1. Make sure you have UV installed
2. Clone or navigate to this project
3. Install dependencies:

```bash
uv sync
```

## Development

This project uses [Ruff](https://github.com/astral-sh/ruff) for code formatting and linting.

**Before committing code changes**, always run:

```bash
# Check and auto-fix linting issues
uv run ruff check . --fix

# Format code
uv run ruff format .

# Run the tests (yt-dlp is replaced by a fake, no network access)
uv run pytest -q
```

This ensures consistent code style and catches common issues.

GitHub Actions runs the same checks on every push to `master` and on pull requests (`.github/workflows/ci.yml`), and also builds the Docker image, checks that yt-dlp in it finds Deno and ffmpeg, and checks that the app starts.

The frontend tests run the page script in Node.js against a stubbed DOM, pytest skips them when `node` is not installed.

The downloads directory defaults to `./downloads` and can be changed with the `DOWNLOADS_DIR` environment variable.

Downloads run in an in-process worker, at most `DOWNLOAD_CONCURRENCY` (default `2`) at the same time; further jobs wait as `pending`. The queue lives in memory: jobs still queued or running when the service stops are marked `interrupted` on the next start.

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

Or using docker-compose:

```bash
docker-compose up -d
```

The application will be available at: http://localhost:8000

**Note:** The `-v $(pwd)/downloads:/app/downloads` flag mounts the downloads directory so your downloads persist outside the container.

### Using Tailscale (Secure Remote Access)

Access your service securely over Tailscale network with automatic HTTPS.

#### Setup

1. **Get Tailscale auth key:**
   - Go to https://login.tailscale.com/admin/settings/keys
   - Click "Generate auth key"
   - Configure: Reusable=Yes, Ephemeral=No, Tags=`tag:service`
   - Copy the generated key

2. **Create `.env` file:**
   ```bash
   cp .env.example .env
   ```
   Add your auth key to `.env`:
   ```
   TS_AUTHKEY=tskey-auth-XXXXXXXXXXXXXXXX-XXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXX
   ```

3. **Start services:**
   ```bash
   docker-compose up -d
   ```
   This starts both the app and Tailscale sidecar.

4. **Find your hostname:**
   Check https://login.tailscale.com/admin/machines for:
   ```
   video-download.your-tailnet-name.ts.net
   ```

5. **Access securely:**
   ```
   https://video-download.your-tailnet-name.ts.net
   ```

#### Architecture

```
Tailscale Network → HTTPS (443) → ts-video-download → HTTP (8000) → video-download app
```

- Automatic HTTPS with valid certificates
- Private access (only Tailscale users)
- No port forwarding needed

#### Troubleshooting

**Container won't start:** Check `/dev/net/tun` exists or load module:
```bash
sudo modprobe tun
```

**Can't connect:** Check logs:
```bash
docker logs ts-video-download
```

**Access from other devices:** Install Tailscale on phone/tablet, sign in, access the URL.

**Enable public access (Funnel):** Edit `ts-video-download/config/mcp.json` and set `"AllowFunnel": true`

## Usage

1. Open http://localhost:8000 in your browser
2. Paste a video URL (e.g., YouTube, Vimeo, etc.)
3. Click "Download"
4. Server returns the download ID immediately and starts the download in the background
5. Browser opens the job page `/jobs/{download_id}`, which polls the status every 5 seconds with a spinner
6. When complete, download links appear
7. Click on file links to download videos

The job page keeps the form, so you can submit another URL from it. Bookmark or share the job page URL to come back to the download later.

### Privacy & Security

- **No public file listing** - Users can only access downloads if they know the UUID
- **UUID-based access control** - Each download directory uses a unique identifier
- **Download ID in the job page URL** - Anyone with the `/jobs/{download_id}` link sees the job and its files

New jobs get a UUIDv7 ID. Its first 48 bits are the creation time, so an ID reveals when the job was created. The other 74 bits are random, which keeps IDs unguessable. Jobs created earlier have UUIDv4 IDs and keep working.

### Handling Age-Restricted/Private Content

Some platforms (like Instagram) may block downloads for age-restricted or private content with errors like:
```
ERROR: [Instagram]: This content may be inappropriate: It's unavailable for certain audiences.
```

**Solution: Use cookies from a logged-in session**

1. **Export cookies from your browser:**
   - Install a browser extension like "Get cookies.txt LOCALLY" (Chrome/Firefox)
   - Log into Instagram in your browser
   - Use the extension to export cookies in Netscape format
   - Save the file as `instagram_cookies.txt`

2. **Add cookies to the application:**
   - Place `instagram_cookies.txt` in the project root directory
   - The application sends it only to Instagram jobs, other sites get no cookies; for YouTube see [YouTube login](#youtube-login-optional-not-enabled-by-default)
   - For Docker: mount it as a volume in `docker-compose.yml`:
     ```yaml
     volumes:
       - ./downloads:/app/downloads
       - ./instagram_cookies.txt:/app/instagram_cookies.txt  # writable, yt-dlp saves refreshed session cookies back
     ```

3. **Restart the application** to apply changes

**Note:** Cookies contain your login session. Keep `instagram_cookies.txt` private and add it to `.gitignore`.

#### YouTube login (optional, not enabled by default)

The service never sends `instagram_cookies.txt` to YouTube. Every YouTube job first runs without cookies, because logged-in cookies make yt-dlp use other YouTube player clients, which can offer fewer formats. Set up a YouTube login only after a YouTube job failed with a login, age or bot check, for example `Sign in to confirm your age` or `Sign in to confirm you’re not a bot`. The page then shows a hint about `youtube_cookies.txt`.

Before you start, know the risk. The yt-dlp wiki warns that YouTube can ban an account that yt-dlp uses. Usually YouTube blocks playback for that account for weeks or months, and the YouTube terms of service also let Google terminate the Google account. Use a secondary account, not your personal one. In the EU, a new account can need age verification (ID, card or selfie) before YouTube plays age-restricted videos.

YouTube rotates the session cookies of a browser session that stays open, so export the cookies from a private window:

1. Open a private (incognito) window and log in to YouTube.
2. In the same tab, open https://www.youtube.com/robots.txt.
3. Export the `youtube.com` cookies in Netscape format with an extension such as "Get cookies.txt LOCALLY".
4. Close the private window. Do not use this session in the browser again.
5. Save the file as `youtube_cookies.txt` next to `cookies.txt` and run `chmod 600 youtube_cookies.txt`.
6. For Docker, add this line to the `volumes` of the `video-download` service in `docker-compose.yml`, then run `docker-compose up -d`:
   ```yaml
   - ./youtube_cookies.txt:/app/youtube_cookies.txt:ro
   ```

The compose file does not have this line by default: if the source file is missing, Docker creates a root-owned directory in its place. The service treats a directory as a missing file. To keep the file somewhere else, set the `YOUTUBE_COOKIES_FILE` environment variable to its path.

How the service uses the file:

- It reads the file at the start of each retry, so you do not have to restart the service after you add or replace it.
- It sends the cookies only on one retry, after the first attempt of a YouTube job failed with `needs_auth`. If the retry also fails, the job shows the error of the retry.
- It never writes the file back. yt-dlp gets the cookies as an in-memory copy.
- The job's `metadata.json` records `used_youtube_cookies: true` when the retry ran.

If `download.log` of the job contains `The provided YouTube account cookies are no longer valid`, YouTube rotated the cookies. Export them again with the steps above.

The file is a full Google session credential. Keep it out of git (`.gitignore` and `.dockerignore` already exclude it) and readable only by you. To revoke the session, sign it out in Google Account → Security → Your devices.

More in the yt-dlp wiki: [Exporting YouTube cookies](https://github.com/yt-dlp/yt-dlp/wiki/Extractors#exporting-youtube-cookies) and [How do I pass cookies to yt-dlp](https://github.com/yt-dlp/yt-dlp/wiki/FAQ#how-do-i-pass-cookies-to-yt-dlp).

### Custom Extractor Configuration

For advanced users who need site-specific tweaks or workarounds:

1. **Copy the example configuration:**
   ```bash
   cp extractor_config.example.py extractor_config.py
   ```

2. **Edit `extractor_config.py`** to add custom extractor arguments or yt-dlp options:
   ```python
   EXTRACTOR_ARGS = {
       'somesite': {
           'skip': ['hls'],  # Skip HLS formats
       },
   }

   YTDLP_OPTS = {
       'retries': 3,
       'socket_timeout': 30,
   }
   ```

3. **Restart the application** to apply changes

The configuration file is automatically loaded if present. This allows you to fix site-specific issues without modifying the main code.

**Note:** `extractor_config.py` is gitignored to keep your customizations private.

Some options are owned by the service and always override `YTDLP_OPTS`: `ignoreerrors` (always off, so failures are reported as failures), `noplaylist`, `restrictfilenames`, `paths`, `outtmpl` and `logger`. The default `format` (`bv*+ba/b`) and `merge_output_format` (`mp4`) can be overridden.

### Garbage collection

The service deletes large media of old jobs, so the downloads directory does not grow forever. It runs once in the background a moment after startup, then every `GC_INTERVAL_HOURS`.

- A media file larger than `GC_MIN_SIZE_MB` is deleted once its job finished more than `GC_MAX_AGE_DAYS` ago. Leftover partial downloads (`*.part`, `*.ytdl`) of such a job go by the same rule.
- Partial downloads of a `failed` job are deleted once the job is older than one day, whatever their size.
- The job's age comes from `completed_at`. Jobs created before that field existed use `updated_at`, then `created_at`. File modification times are never used, yt-dlp can set them from the server's `Last-Modified` header.
- `metadata.json`, `download.log`, subtitles and `info.json` are never deleted, so the job page keeps working. Each deleted file is listed in the job's `deleted_files` with `reason` `gc`.
- Jobs that are `pending` or `downloading` are skipped.

Each deleted file gets one INFO line in the service log, and each run ends with a summary line of the file count and the megabytes freed.

Set these environment variables to change the defaults:

- `GC_ENABLED` - `true`; `false` turns the cleanup off
- `GC_DRY_RUN` - `false`; `true` only logs what a run would delete
- `GC_MAX_AGE_DAYS` - `90`
- `GC_MIN_SIZE_MB` - `5`
- `GC_INTERVAL_HOURS` - `6`

**Note:** On an existing deployment, the first run deletes every file that already matches the rules, which is every media file over 5 MB of every job that finished more than 90 days ago. To see the list first, start once with `GC_DRY_RUN=true` and read the service log.

## Directory Structure

```
video-download/
├── main.py              # Entrypoint, runs the FastAPI app with uvicorn
├── video_download/
│   ├── web.py           # FastAPI app and routes
│   ├── jobs.py          # Job creation, the download task, the startup sweep
│   ├── worker.py        # In-process job queues with a concurrency limit per lane
│   ├── ytdlp.py         # yt-dlp options and the files and metadata a download produced
│   ├── subtitles.py     # Which subtitle tracks a job downloads
│   ├── cleanup.py       # Garbage collection of large old media
│   ├── csrf.py          # CSRF protection of the state-changing routes
│   ├── errors.py        # Classification of failures into error codes
│   ├── urls.py          # URL validation and YouTube playlist stripping
│   ├── storage.py       # metadata.json of each job
│   ├── config.py        # Downloads directory, cookies files, extractor_config.py
│   ├── static/
│   │   └── favicon.svg  # Page icon, also served as /favicon.ico
│   └── templates/
│       └── index.html   # Frontend HTML template
├── tests/               # pytest suite
├── downloads/           # Download directories (created automatically)
│   └── {uuid}/
│       ├── metadata.json    # Download status and video metadata
│       ├── download.log     # yt-dlp output for this download
│       ├── info.json        # yt-dlp's video details
│       ├── Video_Title-id.mp4   # Downloaded video (URL-safe filename)
│       └── Video_Title-id.en.vtt   # Subtitles, one file per track
└── pyproject.toml       # UV project configuration
```

## API Endpoints

### Public Endpoints

- `GET /` - Main web interface
- `GET /jobs/{download_id}` - Web interface for one job, it polls `/status/{download_id}`
  - Accepts any UUID version; 422 for a malformed ID, an unknown ID gets the page with "Download not found"
- `POST /download` - Initiate download (returns UUID immediately)
  - Accepts: `url` form parameter
  - Requires the CSRF token, see below
  - Returns: `{"download_id": "uuid"}`
  - Returns HTTP 400 with `{"detail": "..."}` for a non-http(s) URL or a YouTube playlist/channel link, no job is created
- `GET /status/{download_id}` - Check download status
  - Returns: Status object with download state, files, metadata, errors; 404 for an unknown id, 422 for a malformed one
- `GET /downloads/{download_id}/{filename}` - Download a specific file
  - Serves only the files listed in the job's `artifacts`; `metadata.json` and `download.log` are not public
- `DELETE /downloads/{download_id}/{filename}` - Delete one file of the job's `artifacts`, requires the CSRF token
  - Returns the job's status; records the file in `deleted_files`
  - 404 for an unknown job or a file that is not an artifact (including `metadata.json` and `download.log`), 409 while the job is `pending` or `downloading`

### CSRF protection

Requests that change state need the `X-CSRF-Token` header with the token the page embeds in its `csrf-token` meta tag. The page response also sets that token in the `csrf_token` cookie (HttpOnly, SameSite=Strict, Secure over https), and the server compares the two. Requests with a `Sec-Fetch-Site` other than `same-origin` or `none`, or an `Origin` that differs from the `Host`, are rejected as well. A rejected request gets HTTP 403 with `{"detail": "..."}`. To call the API from a script, first `GET /`, then send the cookie and the token from the page.

### Metadata Structure

Each download creates a `metadata.json` file with:

```json
{
  "download_id": "uuid",
  "url": "original_url",
  "download_url": "url_passed_to_yt_dlp",
  "extractor": "Youtube",
  "status": "completed|downloading|pending|failed",
  "files": ["Video_Title-dQw4w9WgXcQ.mp4"],
  "artifacts": [
    {"name": "Video_Title-dQw4w9WgXcQ.mp4", "kind": "media", "size": 52428800},
    {"name": "Video_Title-dQw4w9WgXcQ.en.vtt", "kind": "subtitle", "size": 41000, "lang": "en", "auto": false},
    {"name": "info.json", "kind": "info", "size": 350000}
  ],
  "subtitles_error": null,
  "deleted_files": [
    {"name": "Video_Title-dQw4w9WgXcQ.en-GB.vtt", "kind": "subtitle", "size": 40000, "deleted_at": "2025-01-15T08:00:00+00:00", "reason": "user"}
  ],
  "error": null,
  "error_code": null,
  "error_type": null,
  "created_at": "2025-01-14T12:00:00",
  "updated_at": "2025-01-14T12:05:00",
  "completed_at": "2025-01-14T12:05:00",
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

`files` lists only the media; `artifacts` lists every downloadable file of the job with its `kind` (`media`, `subtitle`, `info`). For a subtitle, `lang` is yt-dlp's track key (`en`, `en-GB`, `cs-orig`) and `auto` is `true` for automatic captions. `deleted_files` keeps a record of every file removed from `artifacts`; `reason` is `user` for the delete button and `gc` for the [garbage collection](#garbage-collection), which also records leftover partial downloads with `kind` `partial`. Jobs created before artifacts existed have no `artifacts` and no `completed_at`; `/status` lists their media files as artifacts.

YouTube jobs also carry `used_youtube_cookies` (`true` when the retry with `youtube_cookies.txt` ran).

`download_url` differs from `url` when playlist parameters were stripped from a YouTube link. For a failed download, `error` is yt-dlp's message, `error_type` the class of the root exception, and `error_code` one of:

- `rate_limited` - The site throttles requests (YouTube: "try again later"), retry in about an hour
- `needs_auth` - Login, age confirmation or a private video, export `instagram_cookies.txt` (see above), or `youtube_cookies.txt` for YouTube
- `unavailable` - Removed, private or otherwise not available
- `unsupported` - yt-dlp has no extractor for the URL
- `geo_restricted` - Not available from the server's location
- `network` - Connection or HTTP failure
- `postprocessing` - ffmpeg merging or conversion failed
- `interrupted` - The service restarted while the download was running
- `invalid_url` - yt-dlp could not interpret the URL
- `unknown` - Anything else, including a run that produced no media file

## How It Works

1. **Submit** - User submits URL via form
2. **ID Generation** - Server creates a download directory named by a new UUIDv7
3. **Immediate Response** - ID returned to client, HTTP request completes, the browser opens `/jobs/{uuid}`
4. **Job queue** - The job goes into the in-process worker's download queue; at most `DOWNLOAD_CONCURRENCY` downloads run at once, the rest wait as `pending`
5. **Status Updates** - Metadata file updated as download progresses
6. **Polling** - Frontend polls `/status/{uuid}` every 5 seconds
7. **Completion** - Files available via `/downloads/{uuid}/{filename}`

## Technologies Used

- **FastAPI** - Web framework
- **yt-dlp** - Video download library
- **Uvicorn** - ASGI server
- **HTML/CSS/JavaScript** - Frontend with polling
- **Python asyncio** - The job worker, queues with a concurrency limit each
