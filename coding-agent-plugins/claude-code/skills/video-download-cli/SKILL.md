---
name: video-download-cli
description: CLI for the video download service - download a video or post from YouTube, Instagram, Vimeo, TikTok, X and the other sites yt-dlp supports, with its subtitles and details (title, uploader, duration, chapters), and transcribe it. Use when the user wants a video downloaded, wants its subtitles, details or a transcript, or wants to know what is said in a video they link.
trigger-keywords: video, youtube, instagram, download video, subtitles, transcript, transcribe, yt-dlp
allowed-tools: Bash(video-download --help), Bash(video-download --refresh-tools --help), Bash(video-download config:*), Bash(video-download schema:*), Bash(video-download get-job:*), Bash(video-download download-video:*), Bash(video-download transcribe-file --help)
---

# Video download CLI

`video-download` calls the tools of the video download service's MCP server. The service downloads one video or post with yt-dlp, together with its subtitles and its details (`info.json`), and can transcribe a downloaded file with its transcription provider. `video-download --help` lists the tools, `video-download <command> --help` shows a tool's full description and options.

## Setup

The service runs on a private tailnet, so the CLI works only on a machine connected to that tailnet. Install the CLI from a clone of the repository:

```bash
uv tool install -e cli/
```

The CLI reads the service's base URL from `VIDEO_DOWNLOAD_URL` (for example `https://video-download.example.ts.net`, without `/mcp`), or the full MCP URL from `servers.video-download.url` in `~/.config/video-download/config.yaml`. If a command exits 2 and names `VIDEO_DOWNLOAD_URL`, ask the user for the service URL. Never guess it. If a command exits 3, the service is not reachable: tell the user to check the tailnet connection.

## Flow

1. Start the download. Pass a link to ONE video or ONE post. Links to playlists, channels, profiles and other collections are rejected, even when the collection holds one video. A video link that also names a playlist downloads only the video.
   ```bash
   video-download download-video --url "https://www.youtube.com/watch?v=..."
   ```
   The CLI does not support MCP tasks, so the answer is the job at once, usually with `state` `pending`. Note its `download_id`.
2. Follow the job until `done` is `true` (`state` `completed` or `failed`). Each call waits up to 60 seconds for a change, so repeat it:
   ```bash
   video-download get-job --download-id <download_id> --wait-seconds 60
   ```
   A failed job has `error` and `error_code`; report them to the user. Do not start the same download again unless the error says it may help.
3. Read the result. `files` lists every file of the job with its `kind` (`media`, `subtitle`, `info`, `transcript`), `size` and a plain HTTP `url`. The URLs need no authentication. Subtitle files also have `lang` (`en`, `cs-orig`, ...) and `auto_captions`. The video details (title, uploader, duration, chapters) are in the job itself and in `info.json`.
4. Read the subtitles by downloading their URL:
   ```bash
   curl -sS "<subtitle url>"
   ```
5. Transcribe only when the user asks for a transcript, or when the job has no subtitle files or they look broken: truncated, in the wrong language or garbled. A transcription costs money, the price per hour is in `video-download transcribe-file --help`, so ask the user first unless they asked for the transcript.
   ```bash
   video-download transcribe-file --download-id <download_id> --filename <name of a media file>
   ```
   It answers at once. Follow it with `get-job --wait-seconds 60` as in step 2 until the transcription in `transcriptions` is `completed` or `failed`. The transcript is new files of kind `transcript`; read the `.transcript.md` by its URL like the subtitles. The `.transcript.json` has the segments with their times, and an ElevenLabs transcript also has a large `.transcript.words.json` with every word, which you rarely need. A new transcript replaces an earlier one of the same file.
   If `video-download --help` lists no `transcribe-file`, the service has no transcription provider configured and cannot transcribe; tell the user.

To give the user a file, give them its URL. Download the media file only when the task needs it locally.

## Output and errors

- Under an AI agent the result is the job as compact JSON; pipe it into `jq`, for example `jq '.files[] | {name, kind, url}'`.
- The result also lists `video-download://jobs/...` MCP resources (metadata, download log, transcripts). The CLI cannot read MCP resources. Use the file URLs instead: transcripts are files of the job too.
- Exit codes: 0 success; 1 the tool reported an error, such as a rejected link (message on stderr); 2 a usage or config error; 3 the service could not be reached.
- The tool list is cached for an hour; `video-download --refresh-tools --help` fetches it again. `video-download config` shows the effective configuration without contacting the service.
