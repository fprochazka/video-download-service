import pytest
import yt_dlp
from fastapi.testclient import TestClient

from tests import elevenlabs_fakes
from tests.genai_fakes import API_KEY, FakeGenai
from tests.ytdlp_fakes import FakeYoutubeDL
from video_download import audio, config, elevenlabs, gemini, transcription, web
from video_download.csrf import CSRF_COOKIE, CSRF_HEADER

# PUBLIC_BASE_URL of the MCP tests
BASE_URL = 'https://video-download.example.ts.net'

# The settings the service reads from its environment; a value in the developer's environment must never reach a test
SERVICE_ENV = (
    'TRANSCRIPTION_PROVIDER',
    'GEMINI_API_KEY',
    'ELEVENLABS_API_KEY',
    'TRANSCRIPTION_MODEL',
    'TRANSCRIPTION_PRICES',
    'TRANSCRIPTION_CONCURRENCY',
    'TRANSCRIPTION_CHUNK_MINUTES',
    'TRANSCRIPTION_CHUNK_CONCURRENCY',
    'DOWNLOAD_CONCURRENCY',
    'PUBLIC_BASE_URL',
    'MCP_ALLOWED_HOSTS',
    'GC_ENABLED',
    'GC_DRY_RUN',
    'GC_MAX_AGE_DAYS',
    'GC_MIN_SIZE_MB',
    'GC_INTERVAL_HOURS',
)


@pytest.fixture(autouse=True)
def clean_service_env(monkeypatch):
    """Transcription tests set a fake key, the config tests the values they check"""
    for name in SERVICE_ENV:
        monkeypatch.delenv(name, raising=False)


@pytest.fixture
def downloads_dir(tmp_path, monkeypatch):
    directory = tmp_path / 'downloads'
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(config, 'DOWNLOADS_DIR', directory)
    monkeypatch.setattr(config, 'load_user_config', lambda: ({}, {}))
    monkeypatch.setattr(yt_dlp, 'YoutubeDL', FakeYoutubeDL)
    FakeYoutubeDL.behaviour = None
    FakeYoutubeDL.subtitles_behaviour = None
    FakeYoutubeDL.download_behaviour = None
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


@pytest.fixture
def genai(monkeypatch, downloads_dir):
    fake = FakeGenai()
    monkeypatch.setattr(gemini, 'make_client', fake.client)
    monkeypatch.setattr(gemini, 'FILE_POLL_SECONDS', 0)
    monkeypatch.setenv('TRANSCRIPTION_PROVIDER', 'gemini')
    monkeypatch.setenv('GEMINI_API_KEY', API_KEY)
    return fake


@pytest.fixture
def scribe(monkeypatch, downloads_dir):
    """ElevenLabs selected, with its HTTP endpoint faked and the retries without their delays"""
    fake = elevenlabs_fakes.FakeScribe()
    monkeypatch.setattr(elevenlabs, 'http_client', fake.client)
    monkeypatch.setattr(transcription, 'TRANSIENT_RETRY_DELAYS', (0, 0))
    monkeypatch.setenv('TRANSCRIPTION_PROVIDER', 'elevenlabs')
    monkeypatch.setenv('ELEVENLABS_API_KEY', elevenlabs_fakes.API_KEY)
    return fake


@pytest.fixture
def fake_audio(monkeypatch):
    """ffmpeg and ffprobe replaced: the media is 15 s long, the extracted audio a few bytes"""
    extracted = []

    async def extract(media, target):
        extracted.append((media.name, target.name))
        target.write_bytes(b'OggS fake audio')

    async def probe(path):
        return 15.0

    monkeypatch.setattr(audio, 'extract_audio', extract)
    monkeypatch.setattr(audio, 'probe_duration', probe)
    return extracted


@pytest.fixture
def anyio_backend():
    """The async tests run on asyncio, like the app"""
    return 'asyncio'


@pytest.fixture
def public_base_url(monkeypatch):
    monkeypatch.setenv('PUBLIC_BASE_URL', BASE_URL + '/')
    return BASE_URL
