import asyncio
import json
import logging

import httpx
import pytest

from tests.elevenlabs_fakes import API_KEY, HEADERS, answer, error
from tests.test_transcription import JOB_ID, MEDIA, state_of, transcribe, write_job
from tests.ytdlp_fakes import read_metadata
from video_download import audio, elevenlabs, transcription

MD = 'Engine_Talk-abc.transcript.md'
JSON = 'Engine_Talk-abc.transcript.json'
WORDS = 'Engine_Talk-abc.transcript.words.json'


def test_transcribes_the_whole_recording_in_one_request(scribe, client, downloads_dir, fake_audio, caplog):
    caplog.set_level(logging.DEBUG)
    job_dir = write_job(downloads_dir)

    accepted = transcribe(client)

    assert accepted.status_code == 202
    (request,) = scribe.requests
    assert request['url'] == 'https://api.elevenlabs.io/v1/speech-to-text'
    assert request['key'] == API_KEY
    # The only expected language is that of the manual English subtitles
    assert request['fields'] == {
        'model_id': 'scribe_v2',
        'diarize': 'true',
        'timestamps_granularity': 'word',
        'tag_audio_events': 'true',
        'language_code': 'en',
    }
    assert request['files']['file']['type'] == 'audio/ogg'
    assert request['files']['file']['content'] == b'OggS fake audio'

    state = state_of(downloads_dir)
    assert state['status'] == 'completed', state
    assert (state['provider'], state['model']) == ('elevenlabs', 'scribe_v2')
    assert state['files'] == [MD, JSON, WORDS]
    assert state['usage'] == {'audio_seconds': 15.0}
    # The amount of the response headers, next to the estimate of 15 s at $0.22 per hour
    assert state['cost'] == {
        'usd': 0.000364,
        'estimated': False,
        'source': 'provider',
        'estimate_usd': 0.000917,
        'prices': {
            'model': 'scribe_v2',
            'per_hour': 0.22,
            'as_of': '2026-11-01',
            'source': 'https://elevenlabs.io/pricing/api',
        },
    }
    assert state['provider_report'] == {
        'transcription_id': 'tr-123',
        'audio_seconds': 15.0,
        'credits': 1,
        'region': 'us-central1',
        'trace_id': 'trace-456',
        'cost': {'amount': 0.000364, 'currency': 'usd'},
    }
    assert state['warnings'] == []
    metadata = read_metadata(downloads_dir, JOB_ID)
    assert [a['name'] for a in metadata['artifacts'] if a['kind'] == 'transcript'] == [MD, JSON, WORDS]

    markdown = (job_dir / MD).read_text()
    assert markdown.splitlines()[:3] == ['# Engine talk', '', f'Transcribed from {MEDIA} by ElevenLabs scribe_v2.']
    assert '**[00:00] Speaker 1:** Hello and welcome.' in markdown
    assert '**[00:02] Speaker 2:** Thanks for having me.' in markdown
    # The chapters of info.json head the transcript
    assert '## Intro <b>' in markdown

    stored = json.loads((job_dir / JSON).read_text())
    assert stored['provider'] == 'elevenlabs'
    assert (stored['language_requested'], stored['language']) == ('en', 'eng')
    assert stored['provider_report'] == state['provider_report']
    assert stored['cost'] == state['cost']
    assert stored['speakers'] == [{'id': 'speaker_1', 'label': 'Speaker 1'}, {'id': 'speaker_0', 'label': 'Speaker 2'}]
    assert stored['words_file'] == WORDS
    assert stored['segments'] == [
        {'start': 0.5, 'end': 2.1, 'speaker': 'Speaker 1', 'text': 'Hello and welcome.'},
        {'start': 2.4, 'end': 4.5, 'speaker': 'Speaker 2', 'text': 'Thanks for having me.'},
    ]
    assert json.loads((job_dir / WORDS).read_text()) == answer()
    assert API_KEY not in caplog.text
    assert API_KEY not in (job_dir / 'metadata.json').read_text() + (job_dir / JSON).read_text()


def report_of(headers: dict) -> dict:
    return elevenlabs.provider_report(httpx.Headers(headers), {'audio_duration_secs': 5.0135})


def test_the_report_has_what_the_headers_say():
    assert report_of(HEADERS | {'fiat-currency': ' USD '}) == {
        'transcription_id': 'tr-123',
        'audio_seconds': 5.0135,
        'credits': 1,
        'region': 'us-central1',
        'trace_id': 'trace-456',
        'cost': {'amount': 0.000364, 'currency': 'usd'},
    }
    # Missing headers are left out, the transcription_id of the body stands in
    assert elevenlabs.provider_report(httpx.Headers({}), {'transcription_id': 'tr-body'}) == {
        'transcription_id': 'tr-body'
    }
    assert report_of(HEADERS | {'character-cost': '2.5'})['credits'] == 2.5


@pytest.mark.parametrize(
    'headers',
    [
        {'fiat-cost-before-overages': 'n/a'},
        {'fiat-cost-before-overages': 'nan'},
        {'fiat-cost-before-overages': '-0.1'},
        {'fiat-cost-before-overages': ''},
        {'fiat-currency': ''},
        {'fiat-currency': '  '},
    ],
)
def test_a_malformed_amount_or_currency_is_no_reported_cost(headers):
    report = report_of(HEADERS | headers)

    assert 'cost' not in report
    assert report['credits'] == 1


def test_an_unparsable_credit_count_is_left_out():
    assert 'credits' not in report_of(HEADERS | {'character-cost': 'many'})


@pytest.mark.parametrize(
    ('headers', 'reported'),
    [
        ({'fiat-cost-before-overages': None, 'fiat-currency': None}, None),
        ({'fiat-cost-before-overages': 'oops'}, None),
        ({'fiat-currency': 'eur'}, {'amount': 0.000364, 'currency': 'eur'}),
    ],
)
def test_without_an_amount_in_usd_the_cost_is_the_estimate(
    scribe, client, downloads_dir, fake_audio, headers, reported
):
    scribe.headers = {key: value for key, value in (HEADERS | headers).items() if value is not None}
    write_job(downloads_dir)

    transcribe(client)

    state = state_of(downloads_dir)
    assert state['status'] == 'completed'
    assert (state['cost']['usd'], state['cost']['estimated']) == (0.000917, True)
    assert 'source' not in state['cost']
    # An amount in another currency stays in the report
    assert state['provider_report'].get('cost') == reported
    assert state['provider_report']['credits'] == 1


def test_status_offers_transcription_with_the_selected_providers_key(scribe, client, downloads_dir):
    write_job(downloads_dir)

    assert client.get(f'/status/{JOB_ID}').json()['transcription_available'] is True


def test_no_language_hint_when_the_recording_may_be_in_two(scribe, client, downloads_dir, fake_audio):
    write_job(downloads_dir)
    (downloads_dir / JOB_ID / 'info.json').write_text(json.dumps({'id': 'abc', 'language': 'cs'}))

    transcribe(client)

    assert 'language_code' not in scribe.requests[0]['fields']
    assert state_of(downloads_dir)['status'] == 'completed'


def sentence(start: float, speaker_id: str, text: str) -> list[tuple]:
    """One word per 0.3 s from start"""
    return [(start + 0.3 * i, start + 0.3 * i + 0.25, speaker_id, word) for i, word in enumerate(text.split())]


def test_speakers_keep_their_numbers_and_interjections_fold_into_the_turn(scribe, client, downloads_dir, fake_audio):
    job_dir = write_job(downloads_dir)
    scribe.answer = answer(
        [
            *sentence(0.0, 'speaker_3', 'So we started the company in a garage with two friends.'),
            (3.4, 3.8, 'speaker_0', 'Mm-hmm.'),
            *sentence(4.0, 'speaker_3', 'Then it grew [laughs] fast, faster than any of us had thought.'),
            (8.0, 8.4, 'speaker_0', 'No.'),
            *sentence(8.5, 'speaker_3', 'Did you know that?'),
            (9.8, 10.1, 'speaker_0', 'No.'),
            *sentence(10.3, 'speaker_0', 'Yeah, I remember.'),
        ]
    )

    transcribe(client)

    markdown = (job_dir / MD).read_text()
    assert (
        '**[00:00] Speaker 1:** So we started the company in a garage with two friends. (Speaker 2: Mm-hmm.) Then it '
        'grew [laughs] fast, faster than any of us had thought.'
    ) in markdown
    # Too few words after it to be an interjection, and the answer to a question
    assert '**[00:08] Speaker 2:** No.\n\n**[00:08] Speaker 1:** Did you know that?' in markdown
    assert '**[00:09] Speaker 2:** No. Yeah, I remember.' in markdown
    assert 'Short interjections of another speaker are in parentheses' in markdown
    stored = json.loads((job_dir / JSON).read_text())
    assert stored['segments'][1] == {
        'start': 3.4,
        'end': 3.8,
        'speaker': 'Speaker 2',
        'text': 'Mm-hmm.',
        'backchannel': True,
    }
    assert stored['backchannels'] == 1
    assert stored['speakers'][0] == {'id': 'speaker_3', 'label': 'Speaker 1'}


def test_english_numbers_are_written_in_digits():
    convert = elevenlabs.numbers_as_digits

    assert convert('In two thousand and twenty two it fell sixty six percent.', 'eng') == 'In 2022 it fell 66 percent.'
    assert convert('I have fifteen thousand dollars.', 'en') == 'I have 15,000 dollars.'
    assert convert('It sold for five million, a few million, a hundred people.', 'eng') == (
        'It sold for 5 million, a few million, a hundred people.'
    )
    assert convert('One of the things.', 'eng') == 'One of the things.'
    # text2num has no Czech
    assert convert('šedesát šest procent', 'ces') == 'šedesát šest procent'
    assert convert('sixty six', None) == 'sixty six'


def test_a_wrong_script_is_a_warning(scribe, client, downloads_dir, fake_audio):
    scribe.answer = answer([(0.5, 9.0, 'speaker_0', 'Привет и добро пожаловать в нашу передачу о двигателях сегодня.')])
    write_job(downloads_dir)

    transcribe(client)

    state = state_of(downloads_dir)
    assert state['status'] == 'completed'
    assert [w['code'] for w in state['warnings']] == ['wrong_script']
    assert 'English (en)' in state['warnings'][0]['message']


@pytest.mark.parametrize(
    ('reply', 'code', 'message'),
    [
        (error(401, 'invalid_api_key', 'Invalid API key'), 'not_configured', 'ElevenLabs rejected ELEVENLABS_API_KEY'),
        (error(402, 'insufficient_credits'), 'insufficient_credits', 'ElevenLabs API error 402 insufficient_credits'),
        (error(400, 'request_too_large'), 'too_long', 'ElevenLabs API error 400 request_too_large: nope'),
        (error(403, 'model_access_denied'), 'provider_error', 'ElevenLabs API error 403 model_access_denied'),
        (
            httpx.Response(422, json={'detail': [{'loc': ['body', 'model_id'], 'msg': 'unknown model'}]}),
            'provider_error',
            'ElevenLabs API error 422 validation_error: body.model_id: unknown model',
        ),
        (httpx.ReadTimeout('slow'), 'provider_error', 'ElevenLabs did not answer within 30 minutes'),
        (httpx.Response(200, text='<html>'), 'invalid_response', 'other than JSON'),
    ],
)
def test_errors_fail_the_transcription_at_once(scribe, client, downloads_dir, fake_audio, caplog, reply, code, message):
    scribe.answer = reply
    write_job(downloads_dir)

    transcribe(client)

    state = state_of(downloads_dir)
    assert (state['status'], state['error_code']) == ('failed', code)
    assert message in state['error']
    assert len(scribe.requests) == 1
    assert API_KEY not in state['error'] + caplog.text


def test_rate_limits_and_server_errors_are_sent_again(scribe, client, downloads_dir, fake_audio):
    billed = {'transcription-id': 'tr-failed', 'fiat-cost-before-overages': '0.5', 'fiat-currency': 'usd'}
    scribe.answer = [error(503, 'overloaded', headers=billed), httpx.ConnectError('down'), answer()]
    write_job(downloads_dir)

    transcribe(client)

    state = state_of(downloads_dir)
    assert state['status'] == 'completed'
    assert len(scribe.requests) == 3
    # Only the answered request is reported
    assert state['provider_report']['transcription_id'] == 'tr-123'
    assert state['cost']['usd'] == 0.000364


def test_a_rate_limit_that_lasts_fails_as_rate_limited(scribe, client, downloads_dir, fake_audio):
    scribe.answer = [error(429, 'rate_limit_exceeded')] * 3
    write_job(downloads_dir)

    transcribe(client)

    state = state_of(downloads_dir)
    assert (state['error_code'], len(scribe.requests)) == ('rate_limited', 3)
    assert state['error'] == 'ElevenLabs API error 429 rate_limit_exceeded: nope'


def test_a_recording_over_10_hours_is_too_long(scribe, downloads_dir, fake_audio, monkeypatch):
    async def ten_hours_and_a_second(path):
        return 36001.0

    monkeypatch.setattr(audio, 'probe_duration', ten_hours_and_a_second)
    job_dir = write_job(downloads_dir)
    transcription.request_transcription(job_dir, MEDIA, 'scribe_v2')

    asyncio.run(transcription.transcribe_media_task(JOB_ID, MEDIA))

    state = state_of(downloads_dir)
    assert state['error_code'] == 'too_long'
    assert state['error'] == (
        'The recording is 10:00:01 long, ElevenLabs Scribe takes at most 10:00:00 in one request.'
    )
    assert scribe.requests == []


def test_a_gemini_transcript_after_a_scribe_one_replaces_its_files(
    genai, scribe, client, downloads_dir, fake_audio, monkeypatch
):
    write_job(downloads_dir)
    transcribe(client)
    assert state_of(downloads_dir)['files'] == [MD, JSON, WORDS]

    monkeypatch.setenv('TRANSCRIPTION_PROVIDER', 'gemini')
    transcribe(client)

    metadata = read_metadata(downloads_dir, JOB_ID)
    assert metadata['transcriptions'][MEDIA]['provider'] == 'gemini'
    assert [a['name'] for a in metadata['artifacts'] if a['kind'] == 'transcript'] == [MD, JSON]


def test_status_has_the_reported_cost_and_the_report(scribe, client, downloads_dir, fake_audio):
    write_job(downloads_dir)
    transcribe(client)

    state = client.get(f'/status/{JOB_ID}').json()['transcriptions'][MEDIA]

    def without_nulls(entry: dict) -> dict:
        return {key: without_nulls(v) if isinstance(v, dict) else v for key, v in entry.items() if v is not None}

    stored = state_of(downloads_dir)
    assert without_nulls(state['cost']) == stored['cost']
    assert without_nulls(state['provider_report']) == stored['provider_report']
