"""``video-download``: the tools of the video download service's MCP server (download, job state, transcription)."""

from __future__ import annotations

from typing import Any

from mcp_to_cli_kit.cli import main as kit_main

from video_download_cli import DISTRIBUTION

BIN = "video-download"

SERVER = "video-download"

# The service runs on a private tailnet, so its address differs per installation and is not in the code.
SERVICE_URL_VARIABLE = "VIDEO_DOWNLOAD_URL"

DEFAULT_CONFIG: dict[str, Any] = {
    "help": (
        "Video download CLI - Download a video or post from YouTube, Instagram and the other sites yt-dlp supports, "
        "with its subtitles and details, and transcribe it, through the video download service. Each tool is a "
        "command; use 'video-download <command> --help' for its options. Needs the service's URL in "
        f"{SERVICE_URL_VARIABLE} (for example https://video-download.example.ts.net) or in "
        "~/.config/video-download/config.yaml, and a connection to the tailnet the service runs on."
    ),
    "servers": {
        # Expanded when the server is contacted; a url in the config file replaces the whole template.
        SERVER: {"url": "${" + SERVICE_URL_VARIABLE + "}/mcp"},
    },
}


def main() -> None:
    kit_main(BIN, DEFAULT_CONFIG, distribution=DISTRIBUTION)
