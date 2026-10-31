import pytest
import yt_dlp
from fastapi.testclient import TestClient

from tests.ytdlp_fakes import FakeYoutubeDL
from video_download import config, web
from video_download.csrf import CSRF_COOKIE, CSRF_HEADER


@pytest.fixture
def downloads_dir(tmp_path, monkeypatch):
    directory = tmp_path / 'downloads'
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(config, 'DOWNLOADS_DIR', directory)
    monkeypatch.setattr(config, 'load_user_config', lambda: ({}, {}))
    monkeypatch.setattr(yt_dlp, 'YoutubeDL', FakeYoutubeDL)
    # A key in the developer's environment must never reach a test, transcription tests set a fake one
    monkeypatch.delenv('GEMINI_API_KEY', raising=False)
    monkeypatch.delenv('TRANSCRIPTION_MODEL', raising=False)
    FakeYoutubeDL.behaviour = None
    FakeYoutubeDL.subtitles_behaviour = None
    FakeYoutubeDL.instances = []
    return directory


class JobsTestClient(TestClient):
    """Waits after each request until the jobs it queued on the worker ended, so tests see the finished job"""

    def request(self, *args, **kwargs):
        response = super().request(*args, **kwargs)
        self.portal.call(self.app.state.worker.join)
        return response


@pytest.fixture
def client(downloads_dir):
    """A client that loaded the page first, so its requests carry the CSRF cookie and header like the page's do"""
    with JobsTestClient(web.app) as test_client:
        test_client.get('/')
        test_client.headers[CSRF_HEADER] = test_client.cookies[CSRF_COOKIE]
        yield test_client
