import pytest
import yt_dlp
from fastapi.testclient import TestClient

from tests.ytdlp_fakes import FakeYoutubeDL
from video_download import config, web


@pytest.fixture
def downloads_dir(tmp_path, monkeypatch):
    directory = tmp_path / 'downloads'
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(config, 'DOWNLOADS_DIR', directory)
    monkeypatch.setattr(config, 'load_user_config', lambda: ({}, {}))
    monkeypatch.setattr(yt_dlp, 'YoutubeDL', FakeYoutubeDL)
    FakeYoutubeDL.behaviour = None
    FakeYoutubeDL.subtitles_behaviour = None
    FakeYoutubeDL.instances = []
    return directory


@pytest.fixture
def client(downloads_dir):
    with TestClient(web.app) as test_client:
        yield test_client
