# Video Download Service

A self-hosted web service that downloads videos with yt-dlp, with their subtitles and details, and transcribes them with Gemini or ElevenLabs Scribe, one provider per deployment. It has a web page for people and an MCP server for agents, and runs with Docker Compose behind a Tailscale sidecar.

The repository holds two independent uv projects:

- [`service/`](service/README.md) - the web service and its MCP server: features, setup, configuration, API and development
- [`cli/`](cli/README.md) - `video-download`, a command-line client of the MCP server, and the Claude Code skill that teaches an agent to use it (`coding-agent-plugins/claude-code/`, published through `.claude-plugin/marketplace.json`)

To give Claude Code the skill, install the plugin, then install the CLI as described in [`cli/README.md`](cli/README.md):

```
/plugin install video-download-cli --marketplace fprochazka/video-download-service
```

`docs/` has the measured results of the transcription experiments and the research behind the design: [models](docs/transcription-models.md), [chunking](docs/transcription-chunking.md) and [research notes](docs/research.md). [The benchmark methodology](docs/benchmark-methodology.md) describes how they were measured, with the scripts in `benchmarks/` that reproduce them and score new providers. [Ideas and known gaps](docs/ideas.md) lists what is found but not done yet.

## Deployment

`docker-compose.yml` stays at the repository root and builds the image from `service/`. The files it mounts into the container sit next to it: `downloads/`, `instagram_cookies.txt`, `extractor_config.py` (see `extractor_config.example.py`), `app.env` (see `app.env.example`) and `.env` with the Tailscale auth key (see `.env.example`). Set `PUBLIC_BASE_URL` in `app.env` to the service's tailnet URL; the MCP endpoint accepts only that host. Set `TRANSCRIPTION_PROVIDER` (`gemini`, `elevenlabs` or `none`) and the provider's key; a deployment that had only `GEMINI_API_KEY` needs `TRANSCRIPTION_PROVIDER=gemini` now. The service does not start with a setting it cannot use, and `docker compose logs video-download` names it.

```bash
git pull
docker compose build
docker compose up -d
```

The [service README](service/README.md#using-tailscale-secure-remote-access) has the Tailscale setup and the rest.

## Commit hooks

The service runs on a real deployment, so its URLs, job IDs, tailnet hostnames, account addresses, cookies and `app.env` values end up in terminals, logs, scratch files and benchmark runs, and from there they can slip into a commit. `scripts/hooks` holds a pre-commit and a commit-msg hook that refuse a commit when a staged line or the commit message matches a private identifier. They cover both projects. The identifiers live in a file outside the repository, because listing them here would publish the strings the hooks exist to keep out. Set it up once per clone, worktrees share it:

```shell
git config core.hooksPath scripts/hooks
git config hooks.forbiddenPatternsFile ~/.config/git/forbidden-patterns.txt  # any path outside the repo
```

The file holds one extended regex per line, matched case-insensitively; blank lines and `#` comments are ignored. Until `hooks.forbiddenPatternsFile` points at a file with at least one pattern, both hooks refuse every commit and print how to fix it. Docs and tests use placeholders such as `video-download.your-tailnet-name.ts.net`.

A rebase does not run the hooks. After you reword or squash commits in a rebase, check the new messages against the patterns file yourself before you push.
