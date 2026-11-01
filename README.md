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
- **Single videos only** - Links to playlists, channels, profiles, albums and other collections are rejected, even when the collection holds one video. The service asks yt-dlp whether the link's extractor returns only collections, so this works on every site yt-dlp supports. A single post with several videos, like an Instagram carousel, is not a collection and downloads all its items. A YouTube link to a video inside a playlist or mix downloads just that video
- **Error handling** - Failed downloads are tracked in metadata with the error message and a machine-readable `error_code`, and logged to the service log
- **Download status states**:
  - `pending` - Download queued
  - `downloading` - Video being downloaded
  - `completed` - At least one media file was downloaded
  - `failed` - Error occurred, or yt-dlp finished without a media file (details in `error`, `error_code`, `error_type`)
- **Transcription** - A Transcribe button on each media file sends its audio to Google Gemini and adds a Markdown and a JSON transcript with timestamps and speaker labels to the job, see [Transcription](#transcription)
- **MCP server** - Agents such as Claude Code download and transcribe videos through the `/mcp` endpoint, see [MCP server](#mcp-server)
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

The frontend tests run the page script in Node.js against a stubbed DOM, pytest skips them when `node` is not installed. The audio tests skip without `ffmpeg`.

The downloads directory defaults to `./downloads` and can be changed with the `DOWNLOADS_DIR` environment variable.

Downloads run in an in-process worker, at most `DOWNLOAD_CONCURRENCY` (default `2`) at the same time; further jobs wait as `pending`. Transcriptions have their own queue, limited by `TRANSCRIPTION_CONCURRENCY` (default `2`). The queue lives in memory: jobs still queued or running when the service stops are marked `interrupted` on the next start.

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

#### Container user

The app does not run as root. The image defaults to uid:gid `1000:1000`, and `docker-compose.yml` sets `user: "${APP_UID:-1000}:${APP_GID:-1000}"`. The files the app writes to `downloads/` and the `instagram_cookies.txt` that yt-dlp rewrites belong to that uid on the host. If your host user is not 1000, set `APP_UID` and `APP_GID` in `.env` next to `docker-compose.yml`, or pass `--user "$(id -u):$(id -g)"` to `docker run`. The bind-mounted `downloads/` directory and `instagram_cookies.txt` must be writable by that uid.

The uid does not need an `/etc/passwd` entry in the image. `HOME` is `/tmp`, so Deno and yt-dlp keep their caches in the container's `/tmp`.

If an existing deployment ran as root before, its `downloads/` is owned by root and the app cannot write to it. Change the owner once, without sudo on the host, through a root container of the same service:

```bash
docker compose run --rm --no-deps --user 0 --entrypoint chown video-download -R 1000:1000 /app/downloads
```

Use your `APP_UID:APP_GID` in place of `1000:1000` if you changed them. Then start the service as usual.

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

### Transcription

The job page has a Transcribe button on each media file of a completed job. The service then extracts the audio with ffmpeg, sends it to the Gemini Developer API in one request, and stores the transcript next to the media as two files:

- `<media name>.transcript.md` - the title, then one paragraph per speaker turn, for example `**[01:05] Speaker 1:** text`
- `<media name>.transcript.json` - the segments (`start` and `end` in whole seconds, `speaker`, `text`), the model, the token usage and the warnings. Gemini gives only the start of each segment, so a segment's `end` is the start of the next one

The page lists both under Transcripts, with a Show button that displays the Markdown text. Transcribing a file again replaces both files. A 6-minute video takes about 10 seconds to transcribe.

#### Setup

1. Create an API key at https://aistudio.google.com/apikey, in a Google Cloud project with billing enabled. On the free tier Google may use the audio for training, and its terms allow only paid services for apps that serve users in the EEA.
2. Copy the example settings and put the key in `GEMINI_API_KEY`:
   ```bash
   cp app.env.example app.env
   chmod 600 app.env
   ```
3. Restart the service: `docker-compose up -d`. Compose reads `app.env` into the app container; without the file the service starts with transcription off, and the Transcribe button is disabled.

For local development, export the variables before you start uvicorn: `set -a; . ./app.env; set +a`.

`TRANSCRIPTION_MODEL` selects the model, the default is `gemini-3.5-flash-lite`. `app.env` is gitignored and excluded from the Docker image. The service never logs the key.

#### Cost

On `gemini-3.5-flash-lite` a 6-minute video of dense dialogue took 11.3k input tokens (8.9k of them audio, 25 tokens per second) and 4.0k output tokens. At the list price of $0.30 per million input and $2.50 per million output tokens that is $0.013, about $0.13 per hour of speech. The output is most of the bill, so a recording with less speech costs less.

The job page shows the estimated cost next to each transcript, for example `≈ $0.013`, and its tooltip shows the token counts and the prices used. Gemini reports only token counts, never an amount of money. The service multiplies the counts by a table of list prices: the standard paid tier from https://ai.google.dev/gemini-api/docs/pricing, as of 2026-10-30. Audio input and text input are priced apart where a model prices them apart, thinking tokens are priced as output, and the long-context tier of `gemini-3.1-pro-preview` applies above 200k prompt tokens. The estimate does not know about the free tier, discounts or later price changes. A model without a known price, such as `gemini-3.5-flash`, which the pricing page does not list, shows its token count only.

To change a price or add a model, set `TRANSCRIPTION_PRICES` in `app.env` to a JSON object of prices in USD per million tokens, in single quotes so both Compose and a shell read it whole; it replaces the built-in prices of the models it names:

```
TRANSCRIPTION_PRICES='{"gemini-3.5-flash": {"input": 1.50, "output": 9.00, "as_of": "2026-11-01"}}'
```

Each model takes `input` and `output`, and optionally `input_audio` (when audio input costs more than text), `long_context_tokens` with `input_long` and `output_long`, and `as_of`. For an announced price change, give a list of such objects and add `valid_from` (an ISO date) to the later one. A transcription uses the price that applied on the day it ran. Transcriptions made before the cost was recorded get an estimate in `/status` from their stored usage; their usage has no audio count, so the service assumes 25 audio tokens per second of the media.

The default comes from a comparison on that video, scored against its manual subtitles:

- `gemini-3.5-flash-lite` writing `[HH:MM:SS] Speaker 1: text` lines: 96% of the subtitle words, timestamps within 2 seconds, $0.013.
- The same model, and `gemini-3.5-flash` and `gemini-3.1-pro-preview`, answering in a JSON response schema: all three lost a third of the speech or repeated passages, with timestamps up to two minutes late, at $0.02 to $0.14.
- `gemini-3.5-transcribe`: 93% of the words, word timestamps that a speech model computes, $0.036. It takes no prompt, so it cannot use the title, description or subtitles for names, and its docs limit a request with speaker labels to 30 minutes. The service does not support it.

#### What the service sends to Google

- the audio of the media file, mono 16 kHz Opus, uploaded to the Files API and deleted right after the request
- the title, uploader, channel, description and chapters of the video
- the text of one subtitle track of the job (a manual track first, else the speech recognition track), as a reference that may be wrong. Its timestamps are left out: subtitles timed for another edit of the video made Gemini copy their times

The video's own text goes into the prompt in tagged blocks with `<`, `>` and `&` escaped, and the instructions tell the model to treat it as data. A description that contains instructions therefore cannot end its block, but a model can still be influenced by text it reads.

#### Limits of the current version

- The whole recording goes in one request. Long recordings get worse before they hit a limit; only a 6-minute video was tested.
- The output limit of a request (65,536 tokens) is reached at about 50 to 100 minutes of speech. The transcription then fails with `output_truncated`. Cutting the audio into parts is not supported yet.
- Timestamps come from the model and can drift; they are approximate.
- Speaker labels are `Speaker 1`, `Speaker 2` unless the audio or the description names the speaker.

The service checks the result and stores warnings in `warnings`; they do not fail the transcription:

- `ends_early` - the last segment ends long before the end of the media, so speech may be missing or the timestamps drifted
- `beyond_duration` - timestamps past the end of the media
- `timestamps_out_of_order` - segments that end before they start, or out of order
- `repeated_text` - several lines appear twice, Gemini likely repeated a part of the recording
- `low_coverage` - the transcript has less than 60% of the text of the subtitles
- `no_speech` - the transcript is empty
- `unparsed_lines` - lines of the answer that were not `[HH:MM:SS]` transcript lines, left out

A failed transcription has an `error` and one of these `error_code` values:

- `not_configured` - `GEMINI_API_KEY` is not set
- `rate_limited` - Gemini answered HTTP 429, try again later
- `provider_error` - another Gemini API error, or an answer that ended without a reason Gemini documents as complete
- `output_truncated` - the answer hit the output token limit; a cut-off transcript is never stored
- `blocked` - Gemini's safety or recitation filter stopped the answer
- `invalid_response` - the answer had no `[HH:MM:SS]` transcript line
- `audio_extraction_failed` - ffmpeg could not read the audio
- `media_missing` - the media file was deleted before the transcription started
- `interrupted` - the service restarted while the transcription was queued or running
- `unknown` - anything else

### MCP server

The service is also an MCP server, so an agent such as Claude Code can download and transcribe a video on its own. The endpoint is `/mcp` on the same port as the web page, over Streamable HTTP. In production that is `https://video-download.walrus-hops.ts.net/mcp`. To add it to Claude Code:

```bash
claude mcp add --transport http video-download https://video-download.walrus-hops.ts.net/mcp
```

The endpoint has no authentication and no CSRF token, because the service is reachable only on the tailnet. It checks the `Host` header instead, so a web page cannot reach it through DNS rebinding. It accepts `localhost`, the host of `PUBLIC_BASE_URL`, and the hosts in `MCP_ALLOWED_HOSTS`, a comma-separated list. Any other host gets HTTP 421.

Tools:

- `download_video(url, transcribe=false)` - starts a job for a link to one video or one post, with the same URL rules as the web form: a link to a playlist, channel, profile or other collection is a tool error. With `transcribe` set, the service transcribes every media file of the job once the download completes. A client without task support gets the job at once; see [MCP tasks](#mcp-tasks) for the others.
- `get_job(download_id, wait_seconds=0)` - the job's state, the video's title, duration and uploader, the error and `error_code` of a failed job, the files with their kind, size and URL, and each transcription with its state and estimated cost. With `wait_seconds` (0 to 60) the tool answers once the job changes or ends, so an agent needs fewer calls.
- `transcribe_file(download_id, filename)` - transcribes one media file of a completed job, with the same rules as the Transcribe button. It replaces an earlier transcript of the file.

Without `GEMINI_API_KEY` the server offers no transcription: `download_video` has no `transcribe` parameter, there is no `transcribe_file` tool, and neither the descriptions nor the server instructions mention transcription. The service reads the key once at startup, so a change takes a restart. The transcripts resource stays, for transcripts made earlier.

Each tool advertises the shape of its structured content as an `outputSchema`; all three return the same job object.

A job's `state` is `pending`, `downloading`, `transcribing` (the download completed and a transcription of the job is queued or running), `completed` or `failed`. A tool result has the job as structured content, and a short text with the file URLs. Each file with an absolute URL is also a `resource_link`.

Resources, one template each:

- `video-download://jobs/{download_id}/metadata` - the job's `metadata.json`
- `video-download://jobs/{download_id}/log` - yt-dlp's `download.log`
- `video-download://jobs/{download_id}/transcripts/{filename}` - a `.transcript.md` (text/markdown) or `.transcript.json` (application/json) of the job

The file URLs point at `GET /downloads/{download_id}/{filename}`, which needs no authentication; anyone on the tailnet with the URL can fetch the file. The service builds them from `PUBLIC_BASE_URL`, which `docker-compose.yml` sets to the tailnet URL. Without it, the URLs use the `X-Forwarded-Proto` and `X-Forwarded-Host` headers of the MCP request, or else its `Host` header.

#### MCP tasks

A client that supports the MCP tasks extension (`io.modelcontextprotocol/tasks`, protocol 2026-07-28) gets a task from `download_video` instead of the job. The Claude Code CLI does this from v2.1.304 in an interactive session; its VS Code extension and desktop app do not, and neither does a call from a subagent. Claude Code then follows the task on its own and gives Claude the result when the job ends, so Claude does not call `get_job`. A client without the extension gets the job at once and follows it with `get_job`.

- The task ID is the job's `download_id`. `tasks/get` reads the job's `metadata.json`, so a task survives a restart of the service. A job the restart interrupted ends its task with `error_code` `interrupted`.
- While the job runs, the task is `working`, and its `statusMessage` is the job's state, for example `downloading` or `transcribing 1/1`. With `transcribe`, the task ends only after the transcription has ended. The task asks the client to poll every 5 seconds, and its `ttlMs` is null, because the service never deletes a job's metadata.
- When the job ends, the task is `completed`, with the result that `get_job` returns. The result of a failed job has `isError: true` and the job's `error` and `error_code`. The task status stays `completed` because the extension reserves `failed` for protocol errors.
- `tasks/cancel` is acknowledged, but it does not stop the job, because yt-dlp and Gemini cannot be stopped in the middle of a request. The task's `statusMessage` then says that the job keeps running.

### Garbage collection

The service deletes large media of old jobs, so the downloads directory does not grow forever. It runs once in the background a moment after startup, then every `GC_INTERVAL_HOURS`.

- A media file larger than `GC_MIN_SIZE_MB` is deleted once its job finished more than `GC_MAX_AGE_DAYS` ago. Leftover partial downloads (`*.part`, `*.ytdl`) of such a job go by the same rule.
- Partial downloads of a `failed` job are deleted once the job is older than one day, whatever their size.
- The job's age comes from `completed_at`. Jobs created before that field existed use `updated_at`, then `created_at`. File modification times are never used, yt-dlp can set them from the server's `Last-Modified` header.
- `metadata.json`, `download.log`, subtitles, transcripts and `info.json` are never deleted, so the job page keeps working. Each deleted file is listed in the job's `deleted_files` with `reason` `gc`.
- Jobs that are `pending` or `downloading`, or have a transcription queued or running, are skipped.

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
│   ├── mcp_server.py    # The MCP server at /mcp: tools and resources
│   ├── mcp_tasks.py     # MCP tasks of download_video, read from metadata.json
│   ├── status.py        # The job status the API and the MCP tools return
│   ├── jobs.py          # Job creation, the download task, the startup sweep
│   ├── worker.py        # In-process job queues with a concurrency limit per lane
│   ├── transcription.py # Transcription pipeline: prompt, answer parsing, checks, transcript files
│   ├── gemini.py        # Gemini Developer API calls (Files API upload, generate_content)
│   ├── pricing.py       # List prices and the estimated cost of a transcription
│   ├── audio.py         # ffmpeg audio extraction and ffprobe duration
│   ├── vad.py           # Speech and pauses of a recording, from Silero VAD or ffmpeg silencedetect
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
│       ├── Video_Title-id.en.vtt   # Subtitles, one file per track
│       ├── Video_Title-id.transcript.md     # Transcript, see Transcription
│       └── Video_Title-id.transcript.json   # The same transcript as data
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
  - Returns HTTP 400 with `{"detail": "..."}` for a non-http(s) URL or a link to a collection (playlist, channel, profile, album), no job is created
- `GET /status/{download_id}` - Check download status
  - Returns: Status object with download state, files, metadata, errors; 404 for an unknown id, 422 for a malformed one
- `GET /downloads/{download_id}/{filename}` - Download a specific file
  - Serves only the files listed in the job's `artifacts`; `metadata.json` and `download.log` are not public
- `DELETE /downloads/{download_id}/{filename}` - Delete one file of the job's `artifacts`, requires the CSRF token
  - Returns the job's status; records the file in `deleted_files`
  - 404 for an unknown job or a file that is not an artifact (including `metadata.json` and `download.log`), 409 while the job is `pending` or `downloading`, or while the file is being transcribed
  - Deleting a media file keeps its transcripts
- `POST /downloads/{download_id}/{filename}/transcribe` - Queue a transcription of one media file, requires the CSRF token
  - Returns HTTP 202 with the job's status, the file's entry in `transcriptions` is `pending`
  - 404 for an unknown job or a file that is not a media artifact, 409 when the job is not `completed` or the file already has a transcription `pending` or `running`, 503 with `{"detail": "...", "error_code": "not_configured"}` without `GEMINI_API_KEY`

- `POST /mcp` - The MCP server, see [MCP server](#mcp-server); no CSRF token

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
  "auto_transcribe": false,
  "status": "completed|downloading|pending|failed",
  "files": ["Video_Title-dQw4w9WgXcQ.mp4"],
  "artifacts": [
    {"name": "Video_Title-dQw4w9WgXcQ.mp4", "kind": "media", "size": 52428800},
    {"name": "Video_Title-dQw4w9WgXcQ.en.vtt", "kind": "subtitle", "size": 41000, "lang": "en", "auto": false},
    {"name": "info.json", "kind": "info", "size": 350000}
  ],
  "subtitles_error": null,
  "transcriptions": {
    "Video_Title-dQw4w9WgXcQ.mp4": {
      "status": "completed",
      "model": "gemini-3.5-flash-lite",
      "requested_at": "2025-01-14T12:10:00+00:00",
      "started_at": "2025-01-14T12:10:00+00:00",
      "completed_at": "2025-01-14T12:10:25+00:00",
      "error": null,
      "error_code": null,
      "warnings": [],
      "usage": {"prompt_tokens": 11293, "prompt_audio_tokens": 8924, "output_tokens": 4019, "thinking_tokens": 0},
      "cost": {"usd": 0.013435, "estimated": true, "prices": {"model": "gemini-3.5-flash-lite", "input_text": 0.3, "input_audio": 0.3, "output": 2.5, "as_of": "2026-10-30", "source": "https://ai.google.dev/gemini-api/docs/pricing"}},
      "files": ["Video_Title-dQw4w9WgXcQ.transcript.md", "Video_Title-dQw4w9WgXcQ.transcript.json"]
    }
  },
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

`files` lists only the media; `artifacts` lists every downloadable file of the job with its `kind` (`media`, `subtitle`, `info`, `transcript`). A transcript names its media file in `media`. For a subtitle, `lang` is yt-dlp's track key (`en`, `en-GB`, `cs-orig`) and `auto` is `true` for automatic captions. `deleted_files` keeps a record of every file removed from `artifacts`; `reason` is `user` for the delete button and `gc` for the [garbage collection](#garbage-collection), which also records leftover partial downloads with `kind` `partial`. Jobs created before artifacts existed have no `artifacts` and no `completed_at`; `/status` lists their media files as artifacts.

`transcriptions` holds one entry per media file that was ever transcribed, keyed by the media file name; `status` is `pending`, `running`, `completed` or `failed`, see [Transcription](#transcription) for the warnings and error codes. `cost` is the estimate described under [Cost](#cost), `null` for a model without a known price. `/status` also returns `transcription_available`, `true` when the server has a Gemini API key.

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
- **google-genai** - Gemini Developer API client for transcription
- **Uvicorn** - ASGI server
- **HTML/CSS/JavaScript** - Frontend with polling
- **Python asyncio** - The job worker, queues with a concurrency limit each
