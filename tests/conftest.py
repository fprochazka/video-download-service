import pytest
import yt_dlp
from fastapi.testclient import TestClient

import main
from tests.ytdlp_fakes import FakeYoutubeDL


@pytest.fixture
def downloads_dir(tmp_path, monkeypatch):
    directory = tmp_path / 'downloads'
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(main, 'DOWNLOADS_DIR', directory)
    monkeypatch.setattr(main, 'load_user_config', lambda: ({}, {}))
    monkeypatch.setattr(yt_dlp, 'YoutubeDL', FakeYoutubeDL)
    FakeYoutubeDL.behaviour = None
    FakeYoutubeDL.instances = []
    return directory


@pytest.fixture
def client(downloads_dir):
    with TestClient(main.app) as test_client:
        yield test_client
