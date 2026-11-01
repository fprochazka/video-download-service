import logging

import pytest
from fastapi.testclient import TestClient

from tests.genai_fakes import API_KEY
from video_download import config, web


def start(caplog) -> str:
    """Start the app as uvicorn would; the error it refused to start with"""
    caplog.set_level(logging.ERROR)
    with pytest.raises(config.ConfigError) as refused, TestClient(web.app):
        pass
    return str(refused.value)


@pytest.mark.parametrize(
    ('name', 'value', 'message'),
    [
        ('DOWNLOAD_CONCURRENCY', 'two', "DOWNLOAD_CONCURRENCY must be a whole number of at least 1, it is 'two'."),
        (
            'TRANSCRIPTION_CONCURRENCY',
            '0',
            "TRANSCRIPTION_CONCURRENCY must be a whole number of at least 1, it is '0'.",
        ),
        ('TRANSCRIPTION_CHUNK_CONCURRENCY', '1.5', 'TRANSCRIPTION_CHUNK_CONCURRENCY must be a whole number'),
        ('TRANSCRIPTION_CHUNK_MINUTES', '0.5', "TRANSCRIPTION_CHUNK_MINUTES must be a number at least 1, it is '0.5'."),
        ('GC_ENABLED', 'maybe', "GC_ENABLED must be true or false, it is 'maybe'."),
        ('GC_DRY_RUN', 'yes please', 'GC_DRY_RUN must be true or false'),
        ('GC_MAX_AGE_DAYS', '-1', "GC_MAX_AGE_DAYS must be a number at least 0, it is '-1'."),
        ('GC_MIN_SIZE_MB', 'nan', 'GC_MIN_SIZE_MB must be a number at least 0'),
        ('GC_INTERVAL_HOURS', '0', "GC_INTERVAL_HOURS must be a number more than 0, it is '0'."),
        ('PUBLIC_BASE_URL', 'video-download.example.ts.net', 'PUBLIC_BASE_URL must be an absolute http or https URL'),
        ('PUBLIC_BASE_URL', 'ftp://example.com', 'PUBLIC_BASE_URL must be an absolute http or https URL'),
        ('MCP_ALLOWED_HOSTS', 'ok.example, https://bad.example/', "'https://bad.example/' is not one."),
        ('TRANSCRIPTION_PRICES', '{"m": {"output": 1}}', 'TRANSCRIPTION_PRICES is not a valid JSON object of prices'),
    ],
)
def test_an_invalid_setting_stops_the_startup(downloads_dir, monkeypatch, caplog, name, value, message):
    monkeypatch.setenv(name, value)

    assert message in start(caplog)
    assert f'The service cannot start: {name}' in caplog.text


def test_the_error_never_shows_the_key(downloads_dir, monkeypatch, caplog):
    monkeypatch.setenv('TRANSCRIPTION_PROVIDER', 'gemini')
    monkeypatch.setenv('GEMINI_API_KEY', API_KEY)
    monkeypatch.setenv('TRANSCRIPTION_MODEL', 'whisper-1')

    assert 'TRANSCRIPTION_MODEL' in start(caplog)
    assert API_KEY not in caplog.text


def test_valid_settings_start(downloads_dir, monkeypatch):
    for name, value in {
        'DOWNLOAD_CONCURRENCY': '3',
        'TRANSCRIPTION_CHUNK_MINUTES': '2.5',
        'GC_ENABLED': 'off',
        'GC_MIN_SIZE_MB': '0.5',
        'PUBLIC_BASE_URL': 'http://localhost:8000/',
        'MCP_ALLOWED_HOSTS': 'video-download, 100.64.0.1, [::1]:8000, localhost:*',
        'TRANSCRIPTION_PROVIDER': 'gemini',
        'GEMINI_API_KEY': API_KEY,
        'TRANSCRIPTION_MODEL': 'gemini-9-flash',
        'TRANSCRIPTION_PRICES': '{"gemini-9-flash": {"input": 1, "output": 2}}',
    }.items():
        monkeypatch.setenv(name, value)

    with TestClient(web.app) as client:
        assert client.get('/').status_code == 200
    assert config.public_base_url() == 'http://localhost:8000'
    assert config.chunk_minutes() == 2.5


def test_empty_settings_take_their_defaults(downloads_dir, monkeypatch):
    for name in (
        'TRANSCRIPTION_PROVIDER',
        'DOWNLOAD_CONCURRENCY',
        'GC_ENABLED',
        'PUBLIC_BASE_URL',
        'TRANSCRIPTION_MODEL',
        'TRANSCRIPTION_PRICES',
    ):
        monkeypatch.setenv(name, ' ')

    config.validate()
    assert config.download_concurrency() == 2
    assert config.public_base_url() is None
    assert not config.transcription_settings().available


@pytest.mark.parametrize(
    ('env', 'message'),
    [
        (
            {'TRANSCRIPTION_PROVIDER': 'openai'},
            "TRANSCRIPTION_PROVIDER must be gemini, elevenlabs or none, it is 'openai'.",
        ),
        (
            {'TRANSCRIPTION_PROVIDER': 'gemini', 'ELEVENLABS_API_KEY': API_KEY},
            'TRANSCRIPTION_PROVIDER is gemini, so GEMINI_API_KEY must be set; set TRANSCRIPTION_PROVIDER=none to run '
            'without transcription.',
        ),
        ({'TRANSCRIPTION_PROVIDER': 'elevenlabs', 'GEMINI_API_KEY': API_KEY}, 'so ELEVENLABS_API_KEY must be set'),
        (
            {
                'TRANSCRIPTION_PROVIDER': 'elevenlabs',
                'ELEVENLABS_API_KEY': API_KEY,
                'TRANSCRIPTION_MODEL': 'gemini-3.8-flash',
            },
            'TRANSCRIPTION_MODEL must be a model of ElevenLabs Scribe, scribe_..., because TRANSCRIPTION_PROVIDER is '
            "elevenlabs; it is 'gemini-3.8-flash'.",
        ),
        (
            {'TRANSCRIPTION_PROVIDER': 'gemini', 'GEMINI_API_KEY': API_KEY, 'TRANSCRIPTION_MODEL': 'scribe_v2'},
            'TRANSCRIPTION_MODEL must be a model of Gemini, gemini-...',
        ),
    ],
)
def test_an_invalid_provider_setting_stops_the_startup(downloads_dir, monkeypatch, caplog, env, message):
    for name, value in env.items():
        monkeypatch.setenv(name, value)

    assert message in start(caplog)
    assert API_KEY not in caplog.text


@pytest.mark.parametrize(
    ('env', 'provider', 'model', 'available'),
    [
        ({}, None, None, False),
        ({'TRANSCRIPTION_PROVIDER': 'none', 'GEMINI_API_KEY': API_KEY}, None, None, False),
        ({'TRANSCRIPTION_PROVIDER': 'gemini', 'GEMINI_API_KEY': API_KEY}, 'gemini', 'gemini-3.5-flash-lite', True),
        (
            {'TRANSCRIPTION_PROVIDER': 'elevenlabs', 'ELEVENLABS_API_KEY': API_KEY, 'GEMINI_API_KEY': 'other'},
            'elevenlabs',
            'scribe_v2',
            True,
        ),
    ],
)
def test_the_provider_selects_the_key_and_the_default_model(
    downloads_dir, monkeypatch, env, provider, model, available
):
    for name, value in env.items():
        monkeypatch.setenv(name, value)

    with TestClient(web.app) as client:
        assert client.get('/').status_code == 200
    settings = config.transcription_settings()
    assert (settings.provider, settings.model, settings.available) == (provider, model, available)
    assert settings.api_key == (API_KEY if available else None)
