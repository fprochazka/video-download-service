# Video Download Service

A self-hosted web service that downloads videos with yt-dlp, with their subtitles and details, and transcribes them with Gemini. It has a web page for people and an MCP server for agents, and runs with Docker Compose behind a Tailscale sidecar.

The repository holds two independent uv projects:

- [`service/`](service/README.md) - the web service and its MCP server: features, setup, configuration, API and development
- [`cli/`](cli/README.md) - `video-download`, a command-line client of the MCP server, and the Claude Code skill that teaches an agent to use it (`coding-agent-plugins/claude-code/`, published through `.claude-plugin/marketplace.json`)

To give Claude Code the skill, install the plugin, then install the CLI as described in [`cli/README.md`](cli/README.md):

```
/plugin install video-download-cli --marketplace fprochazka/video-download-service
```

`docs/` has the measured results of the transcription experiments and the research behind the design: [models](docs/transcription-models.md), [chunking](docs/transcription-chunking.md) and [research notes](docs/research.md).

## Deployment

`docker-compose.yml` stays at the repository root and builds the image from `service/`. The files it mounts into the container sit next to it: `downloads/`, `instagram_cookies.txt`, `extractor_config.py` (see `extractor_config.example.py`), `app.env` (see `app.env.example`) and `.env` with the Tailscale auth key (see `.env.example`). Set `PUBLIC_BASE_URL` in `app.env` to the service's tailnet URL; the MCP endpoint accepts only that host.

```bash
git pull
docker compose build
docker compose up -d
```

The [service README](service/README.md#using-tailscale-secure-remote-access) has the Tailscale setup and the rest.
