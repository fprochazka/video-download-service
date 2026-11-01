# Video download CLI

A command-line interface for the [video download service](../service/README.md) through its MCP server at `/mcp`. Each tool of the server is a command: `download-video` starts a job for one video or post, `get-job` shows its state and files, and `transcribe-file` transcribes a downloaded file with Gemini.

Built on [mcp-to-cli-kit](https://pypi.org/project/mcp-to-cli-kit/), which turns every MCP tool into a subcommand with typed options compiled from the tool's input schema.

## Installation

Requires Python 3.11+ and uv. The kit comes from PyPI as a regular dependency.

```bash
git clone https://github.com/fprochazka/video-download-service.git
cd video-download-service

# Install globally (editable - updates after git pull)
uv tool install -e cli/
```

## Configuration

The service runs on a private tailnet and has no authentication, so the CLI needs no key, but it works only on a machine connected to that tailnet. It needs the service's URL, which differs per installation. Either export the base URL, without `/mcp`:

```bash
export VIDEO_DOWNLOAD_URL="https://video-download.your-tailnet-name.ts.net"
```

or put the full MCP URL in `~/.config/video-download/config.yaml` (or a file passed with `--config PATH`):

```yaml
servers:
  video-download:
    url: https://video-download.your-tailnet-name.ts.net/mcp
```

The built-in default is `${VIDEO_DOWNLOAD_URL}/mcp`, with the variable expanded when the service is contacted. A `url` in the file replaces that template, so the file wins over the variable. Without either, a command that contacts the service exits 2 and names the variable.

Other keys and their environment variables:

```yaml
servers:
  video-download:
    timeout: 30            # seconds to connect and to send a request
    sse_read_timeout: 100  # seconds to wait for an answer
```

```bash
export VIDEO_DOWNLOAD_SERVERS__VIDEO_DOWNLOAD__URL=https://video-download.your-tailnet-name.ts.net/mcp
```

The 100 s answer timeout is longer than the longest wait of `get-job` (60 s). `video-download config` prints the effective configuration without contacting the service.

## Usage

```bash
video-download --help                                   # the tools
video-download download-video --help                    # one tool's full description and options

video-download download-video --url "https://www.youtube.com/watch?v=jNQXAC9IVRw"
video-download get-job --download-id <download_id> --wait-seconds 60
video-download transcribe-file --download-id <download_id> --filename <media file name>
```

- `download-video` answers at once with the job, usually `pending`, because the kit's client does not use the MCP tasks extension. Repeat `get-job --wait-seconds 60` until `done` is `true`. Follow `transcribe-file` the same way.
- The job lists every file with a plain HTTP URL that needs no authentication; download subtitles and transcripts with `curl`. The CLI cannot read the `video-download://` MCP resources the result names.
- `transcribe-file` costs money, about $0.13 per hour of speech. The service offers it only when it has a Gemini key.
- Under an AI agent, or with `--json`, the result is printed exactly as the server sent it. Otherwise, or with `--pretty`, JSON is indented.
- Exit codes: 0 success, 1 the tool reported an error, 2 a usage or config error, 3 the service could not be reached.
- The tool list is cached for an hour in `~/.cache/video-download/video-download.json`; `--refresh-tools` fetches it again. `-v` and `-vv` log to stderr.

## Claude Code skill

The repository is also a Claude Code plugin marketplace (`fprochazka-video-download-service`) with one plugin, `video-download-cli`. Its skill teaches an agent the download flow: start the job, follow it with `get-job`, read the subtitles from their URLs, and transcribe only when the subtitles are missing or broken or the user asks for a transcript.

```bash
claude plugin marketplace add https://github.com/fprochazka/video-download-service.git
claude plugin install video-download-cli@fprochazka-video-download-service
```

The skill allows `--help`, `config`, `schema`, `get-job` and `download-video` without a prompt; a download costs nothing. `transcribe-file` asks for permission each time, because it costs money.

## Development

```bash
cd cli
uv sync
uv run ruff check . && uv run ruff format --check . && uv run mypy --strict src tests && uv run pytest
```

To develop against a local checkout of the kit, run `uv pip install -e <path-to-mcp-to-cli-kit>` after `uv sync`; the next `uv sync` restores the PyPI release.
