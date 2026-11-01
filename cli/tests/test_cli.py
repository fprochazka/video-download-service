"""The bin builds, its default config validates and the service URL comes from the environment. No network."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from click.testing import CliRunner
from mcp_to_cli_kit.cli import build_cli
from mcp_to_cli_kit.client import distribution_version
from mcp_to_cli_kit.config import ConfigError, expand_server, load_config, save_config

from video_download_cli import DISTRIBUTION
from video_download_cli.cli import BIN, DEFAULT_CONFIG, SERVER, SERVICE_URL_VARIABLE

SERVICE_URL = "https://video-download.example.ts.net"


@pytest.fixture(autouse=True)
def home(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    monkeypatch.setenv("HOME", str(tmp_path))
    for name in ("XDG_CONFIG_HOME", "XDG_CACHE_HOME", "CLAUDECODE", SERVICE_URL_VARIABLE):
        monkeypatch.delenv(name, raising=False)
    return tmp_path


def config_file(home: Path, content: dict[str, object]) -> Path:
    return save_config(home / ".config" / "video-download" / "config.yaml", content)


def invoke(*args: str, env: dict[str, str] | None = None) -> tuple[int, str, str]:
    result = CliRunner().invoke(build_cli(BIN, DEFAULT_CONFIG, retry_delays=(0.0, 0.0)), list(args), env=env)
    return result.exit_code, result.stdout, result.stderr


def test_default_config_validates() -> None:
    loaded = load_config(BIN, default=DEFAULT_CONFIG, environ={})

    server = loaded.config.servers[SERVER]
    assert server.url == "${VIDEO_DOWNLOAD_URL}/mcp"
    assert server.mount == "root"
    assert server.headers == {}
    assert loaded.env_prefix == "VIDEO_DOWNLOAD_"
    assert loaded.path == Path.home() / ".config" / "video-download" / "config.yaml"


def test_the_url_comes_from_video_download_url() -> None:
    environ = {SERVICE_URL_VARIABLE: SERVICE_URL}
    server = load_config(BIN, default=DEFAULT_CONFIG, environ=environ).config.servers[SERVER]

    assert expand_server(SERVER, server, environ).url == f"{SERVICE_URL}/mcp"


def test_video_download_url_is_not_read_as_a_config_override() -> None:
    loaded = load_config(BIN, default=DEFAULT_CONFIG, environ={SERVICE_URL_VARIABLE: SERVICE_URL})

    assert loaded.env_applied == ()


def test_a_missing_url_names_the_variable_without_contacting_the_service() -> None:
    server = load_config(BIN, default=DEFAULT_CONFIG, environ={}).config.servers[SERVER]

    with pytest.raises(ConfigError, match="uses the environment variable VIDEO_DOWNLOAD_URL, which is not set"):
        expand_server(SERVER, server, {})


def test_a_missing_url_fails_a_command_with_a_config_error() -> None:
    code, _, stderr = invoke("get-job", "--download-id", "01a1968d-3dc0-725e-b64f-b7790a0de14a")

    assert code == 2
    assert "VIDEO_DOWNLOAD_URL" in stderr


def test_the_config_file_url_replaces_the_template(home: Path) -> None:
    path = config_file(home, {"servers": {SERVER: {"url": f"{SERVICE_URL}/mcp"}}})

    server = load_config(BIN, default=DEFAULT_CONFIG, config_path=path, environ={}).config.servers[SERVER]

    assert expand_server(SERVER, server, {}).url == f"{SERVICE_URL}/mcp"


def test_config_shows_the_template_unexpanded() -> None:
    code, stdout, _ = invoke("config", "--json", env={SERVICE_URL_VARIABLE: SERVICE_URL})

    assert code == 0
    assert json.loads(stdout)["config"]["servers"][SERVER]["url"] == "${VIDEO_DOWNLOAD_URL}/mcp"


def test_bin_builds_and_shows_its_version() -> None:
    result = CliRunner().invoke(build_cli(BIN, DEFAULT_CONFIG, distribution=DISTRIBUTION), ["--version"])

    assert result.exit_code == 0
    assert result.stdout.startswith(f"video-download {distribution_version(DISTRIBUTION)} (mcp-to-cli-kit ")
