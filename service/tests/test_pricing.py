import json
from datetime import date

import pytest

from tests.ytdlp_fakes import read_metadata
from video_download import config, pricing, storage

DAY = date(2026, 10, 31)


@pytest.fixture(autouse=True)
def built_in_prices(monkeypatch):
    monkeypatch.delenv('TRANSCRIPTION_PRICES', raising=False)


# The usage of a real 6-minute transcription on gemini-3.5-flash-lite
USAGE = {'prompt_tokens': 11293, 'prompt_audio_tokens': 8924, 'output_tokens': 4019, 'thinking_tokens': 0}


def approx(value):
    """The cost is stored rounded to a millionth of a dollar"""
    return pytest.approx(value, abs=1e-6)


def usd(model, usage=USAGE, on=DAY):
    cost = pricing.estimate(model, [usage], on)
    return None if cost is None else cost['usd']


def test_one_price_for_every_modality():
    assert usd('gemini-3.5-flash-lite') == approx((11293 * 0.30 + 4019 * 2.50) / 1e6)


def test_audio_and_text_input_are_priced_apart():
    expected = (2369 * 0.25 + 8924 * 0.50 + 4019 * 1.50) / 1e6

    assert usd('gemini-3.1-flash-lite') == approx(expected)
    cost = pricing.estimate('gemini-3.1-flash-lite', [USAGE], DAY)
    assert cost['prices'] | {'source': None} == {
        'model': 'gemini-3.1-flash-lite',
        'input_text': 0.25,
        'input_audio': 0.50,
        'output': 1.50,
        'as_of': '2026-10-30',
        'source': None,
    }


def test_thinking_tokens_are_billed_as_output():
    usage = USAGE | {'thinking_tokens': 8729}

    assert usd('gemini-3.8-flash', usage) == approx((11293 * 0.75 + (4019 + 8729) * 3.75) / 1e6)


def test_announced_price_change_applies_from_its_date():
    assert usd('gemini-3.8-flash', on=date(2026, 12, 31)) == approx((11293 * 0.75 + 4019 * 3.75) / 1e6)
    assert usd('gemini-3.8-flash', on=date(2027, 1, 1)) == approx((11293 * 1.50 + 4019 * 7.50) / 1e6)


def test_long_context_tier_prices_the_whole_request():
    short = USAGE | {'prompt_tokens': 200_000, 'prompt_audio_tokens': 190_000}
    long = USAGE | {'prompt_tokens': 200_001, 'prompt_audio_tokens': 190_000}

    assert usd('gemini-3.1-pro-preview', short) == approx((200_000 * 2 + 4019 * 12) / 1e6)
    assert usd('gemini-3.1-pro-preview', long) == approx((200_001 * 4 + 4019 * 18) / 1e6)


def test_requests_are_priced_one_by_one_and_summed():
    half = {'prompt_tokens': 150_000, 'prompt_audio_tokens': 150_000, 'output_tokens': 1000, 'thinking_tokens': 0}

    cost = pricing.estimate('gemini-3.1-pro-preview', [half, half], DAY)

    # Each request stays under the 200k tier, their sum would not
    assert cost['usd'] == approx(2 * (150_000 * 2 + 1000 * 12) / 1e6)


@pytest.mark.parametrize('model', ['gemini-3.5-flash', 'unknown-model', None])
def test_unknown_model_has_no_cost(model):
    assert pricing.estimate(model, [USAGE], DAY) is None


def test_env_replaces_and_adds_prices(monkeypatch):
    monkeypatch.setenv(
        'TRANSCRIPTION_PRICES',
        json.dumps(
            {
                'gemini-3.5-flash-lite': {'input': 1.0, 'output': 10.0, 'as_of': '2027-02-01'},
                'gemini-3.5-flash': [
                    {'input': 1.5, 'output': 9.0, 'input_audio': 3.0},
                    {'input': 3.0, 'output': 18.0, 'valid_from': '2027-01-01'},
                ],
            }
        ),
    )

    flash_lite = pricing.estimate('gemini-3.5-flash-lite', [USAGE], DAY)
    assert flash_lite['usd'] == approx((11293 * 1.0 + 4019 * 10.0) / 1e6)
    assert (flash_lite['prices']['as_of'], flash_lite['prices']['source']) == ('2027-02-01', 'TRANSCRIPTION_PRICES')
    assert usd('gemini-3.5-flash') == approx((2369 * 1.5 + 8924 * 3.0 + 4019 * 9.0) / 1e6)
    assert usd('gemini-3.5-flash', on=date(2027, 3, 1)) == approx((11293 * 3.0 + 4019 * 18.0) / 1e6)
    # Models the variable does not name keep the built-in prices
    assert usd('gemini-3.1-flash-lite') is not None


@pytest.mark.parametrize(
    'value', ['not json', '[1, 2]', '{"m": {"output": 1}}', '{"m": {"input": "x", "output": 1, "x": {}}}']
)
def test_invalid_env_prices_are_a_config_error(monkeypatch, value):
    monkeypatch.setenv('TRANSCRIPTION_PRICES', value)

    with pytest.raises(config.ConfigError, match='^TRANSCRIPTION_PRICES is not a valid JSON object of prices'):
        pricing.prices()


def test_scribe_is_priced_per_second_of_audio():
    cost = pricing.estimate('scribe_v2', [{'audio_seconds': 3600}], DAY)

    assert cost == {
        'usd': 0.22,
        'estimated': True,
        'prices': {'model': 'scribe_v2', 'per_hour': 0.22, 'as_of': '2026-11-01', 'source': pricing.ELEVENLABS_SOURCE},
    }
    # A started second is billed whole
    assert usd('scribe_v2', {'audio_seconds': 356.2}) == approx(357 * 0.22 / 3600)


def test_env_sets_a_price_per_hour(monkeypatch):
    monkeypatch.setenv('TRANSCRIPTION_PRICES', '{"scribe_v2": {"per_hour": 0.40, "as_of": "2027-01-01"}}')

    cost = pricing.estimate('scribe_v2', [{'audio_seconds': 1800}], DAY)

    assert cost['usd'] == approx(0.20)
    assert (cost['prices']['per_hour'], cost['prices']['source']) == (0.40, 'TRANSCRIPTION_PRICES')


def test_a_price_without_input_output_or_per_hour_is_a_config_error(monkeypatch):
    monkeypatch.setenv('TRANSCRIPTION_PRICES', '{"scribe_v3": {"as_of": "2027-01-01"}}')

    with pytest.raises(config.ConfigError, match='a price needs input and output, or per_hour'):
        pricing.prices()


def test_stored_usage_without_audio_count_assumes_25_tokens_per_second():
    legacy = {'prompt_tokens': 11293, 'output_tokens': 4019, 'thinking_tokens': 0}

    with_duration = pricing.estimate_stored('gemini-3.1-flash-lite', legacy, 357, DAY)
    without_duration = pricing.estimate_stored('gemini-3.1-flash-lite', legacy, None, DAY)

    assert with_duration['usd'] == approx((2368 * 0.25 + 8925 * 0.50 + 4019 * 1.50) / 1e6)
    assert without_duration['usd'] == approx((11293 * 0.50 + 4019 * 1.50) / 1e6)
    assert with_duration['estimated'] is True
    assert pricing.estimate_stored('gemini-3.1-flash-lite', None, 357, DAY) is None


def test_status_estimates_the_cost_of_transcriptions_stored_without_one(client, downloads_dir):
    job_id = '019a3b2c-4d5e-7f00-8a1b-2c3d4e5f6a7b'
    job_dir = downloads_dir / job_id
    job_dir.mkdir(parents=True)
    (job_dir / 'a.mp4').write_bytes(b'video')
    usage = {'prompt_tokens': 11293, 'output_tokens': 4019, 'thinking_tokens': 0}
    storage.save_metadata(
        job_dir,
        {
            'download_id': job_id,
            'url': 'https://example.com',
            'status': 'completed',
            'artifacts': [{'name': 'a.mp4', 'kind': 'media', 'size': 5}],
            'duration': 357,
            'created_at': '2026-10-31T10:00:00+00:00',
            'updated_at': '2026-10-31T10:00:00+00:00',
            'transcriptions': {
                'a.mp4': {'status': 'completed', 'model': 'gemini-3.5-flash-lite', 'usage': usage, 'files': []},
                'b.mp4': {'status': 'completed', 'model': 'some-future-model', 'usage': usage, 'files': []},
                'c.mp4': {'status': 'failed', 'model': 'gemini-3.5-flash-lite', 'usage': None, 'files': []},
            },
        },
    )

    transcriptions = client.get(f'/status/{job_id}').json()['transcriptions']

    assert transcriptions['a.mp4']['cost']['usd'] == approx((11293 * 0.30 + 4019 * 2.50) / 1e6)
    assert transcriptions['a.mp4']['cost']['estimated'] is True
    assert transcriptions['a.mp4']['usage']['prompt_audio_tokens'] is None
    assert transcriptions['b.mp4']['cost'] is None
    assert transcriptions['c.mp4']['cost'] is None
    # Only computed for the response, metadata.json stays as it was
    assert 'cost' not in read_metadata(downloads_dir, job_id)['transcriptions']['a.mp4']
