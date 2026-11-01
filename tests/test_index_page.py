import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest

from video_download import web

HARNESS = Path(__file__).parent / 'index_page_harness.js'
JOB_ID = '019a3b2c-4d5e-7f00-8a1b-2c3d4e5f6a7b'

pytestmark = pytest.mark.skipif(shutil.which('node') is None, reason='node is not installed')


@pytest.fixture(scope='module')
def page_script(tmp_path_factory) -> Path:
    html = (web.TEMPLATES_DIR / 'index.html').read_text(encoding='utf-8')
    assert '{{ csrf_token }}' in html
    (script,) = re.findall(r'<script>(.*?)</script>', html, re.DOTALL)
    path = tmp_path_factory.mktemp('index_page') / 'index.js'
    path.write_text(script, encoding='utf-8')
    return path


def run_scenario(page_script: Path, scenario: str) -> dict:
    completed = subprocess.run(
        ['node', str(HARNESS), str(page_script), scenario], capture_output=True, text=True, timeout=30, check=True
    )
    return json.loads(completed.stdout.strip().splitlines()[-1])


def test_page_script_is_valid_javascript(page_script):
    subprocess.run(['node', '--check', str(page_script)], capture_output=True, text=True, timeout=30, check=True)


def test_submit_opens_the_job_page(page_script):
    result = run_scenario(page_script, 'submit')

    assert result['fetches'] == [
        {
            'url': '/download',
            'method': 'POST',
            'body': [['url', 'https://www.youtube.com/watch?v=dQw4w9WgXcQ']],
            'csrf': 'page-token',
        }
    ]
    assert result['assigned'] == [f'/jobs/{JOB_ID}']


def test_job_page_polls_the_status_of_the_job_in_the_path(page_script):
    result = run_scenario(page_script, 'job-page')

    assert result['fetches'] == [{'url': f'/status/{JOB_ID}', 'method': 'GET', 'body': None, 'csrf': None}]
    assert result['intervals'] == 1
    assert result['status']['className'] == 'status loading'
    assert 'Some video' in result['status']['text']
    assert result['submitDisabled'] is False


def test_job_page_of_unknown_job_says_not_found(page_script):
    result = run_scenario(page_script, 'job-not-found')

    assert result['status'] == {'className': 'status error', 'text': 'Error: Download not found.'}


def test_completed_job_lists_files_grouped_with_download_and_delete(page_script):
    result = run_scenario(page_script, 'job-completed')

    assert result['status']['className'] == 'status success'
    assert result['groups'] == ['Media', 'Subtitles', 'Details']
    evil = '&quot;&gt;&lt;img src=x onerror=alert(1)&gt;.mp4'
    assert result['links'] == [
        f'/downloads/{JOB_ID}/Some_video-abc.mp4',
        f'/downloads/{JOB_ID}/%22%3E%3Cimg%20src%3Dx%20onerror%3Dalert(1)%3E.mp4',
        f'/downloads/{JOB_ID}/Some_video-abc.en.vtt',
        f'/downloads/{JOB_ID}/Some_video-abc.cs-orig.vtt',
        f'/downloads/{JOB_ID}/info.json',
    ]
    assert result['rows'] == [
        '|Some_video-abc.mp4||50.0 MB||[Download][Transcribe][Delete Some_video-abc.mp4]|',
        f'|{evil}||3 B||[Download][Transcribe][Delete {evil}]|',
        '|Some_video-abc.en.vtt||en · 40.0 KB||[Download][Delete Some_video-abc.en.vtt]|',
        '|Some_video-abc.cs-orig.vtt||cs-orig, auto · 38.1 KB||[Download][Delete Some_video-abc.cs-orig.vtt]|',
        '|info.json||341.8 KB||[Download][Delete info.json]|',
    ]
    assert len(result['deleted']) == 1
    assert result['deleted'][0].startswith('Old-abc.en.vtt: deleted ')
    assert result['deleted'][0].endswith(' by the automatic cleanup')
    assert '<img' not in result['html']
    assert '<b>video</b>' not in result['html']


def test_delete_asks_then_sends_the_csrf_token_and_shows_the_deleted_file(page_script):
    result = run_scenario(page_script, 'delete-file')

    assert result['confirms'] == ['Delete Some_video-abc.mp4? This cannot be undone.']
    delete = {
        'url': f'/downloads/{JOB_ID}/Some_video-abc.mp4',
        'method': 'DELETE',
        'body': None,
        'csrf': 'page-token',
    }
    assert result['fetches'][1] == delete
    assert result['fetches'][2]['url'] == f'/status/{JOB_ID}'
    assert result['groups'] == ['Media', 'Subtitles', 'Details']
    assert [d.split(':')[0] for d in result['deleted']] == ['Some_video-abc.mp4', 'Old-abc.en.vtt']
    assert result['alerts'] == []


def test_cancelled_delete_sends_nothing(page_script):
    result = run_scenario(page_script, 'delete-cancelled')

    assert [f['method'] for f in result['fetches']] == ['GET']


def test_refused_delete_tells_the_user(page_script):
    result = run_scenario(page_script, 'delete-refused')

    assert result['alerts'] == ['The download is still running.']
    assert len(result['rows']) == 5


UNAVAILABLE = 'Transcription is not configured on the server (GEMINI_API_KEY, see README)'
EVIL = '&quot;&gt;&lt;img src=x onerror=alert(1)&gt;.mp4'


def transcribe_buttons(result):
    return [b for b in result['buttons'] if b['text'].startswith('Transcrib')]


def test_transcribe_is_disabled_when_the_server_cannot_transcribe(page_script):
    result = run_scenario(page_script, 'job-completed')

    assert transcribe_buttons(result) == [
        {'text': 'Transcribe', 'disabled': True, 'title': UNAVAILABLE, 'transcribe': None},
        {'text': 'Transcribe', 'disabled': True, 'title': UNAVAILABLE, 'transcribe': None},
    ]


def test_running_transcription_shows_its_state_and_keeps_polling(page_script):
    result = run_scenario(page_script, 'transcription-running')

    assert result['status']['className'] == 'status success'
    assert transcribe_buttons(result) == [
        {'text': 'Transcribing...', 'disabled': True, 'title': None, 'transcribe': 'Some_video-abc.mp4'},
        {'text': 'Transcribing...', 'disabled': True, 'title': None, 'transcribe': EVIL},
    ]
    assert result['transcriptions'] == [
        '||Transcribing with Gemini, this takes a few minutes...',
        '||Transcription queued...',
    ]
    assert (result['intervals'], result['clears']) == (1, 0)


def test_failed_transcription_shows_the_error_escaped_with_a_hint_and_what_it_cost(page_script):
    result = run_scenario(page_script, 'transcription-failed')

    (state,) = result['transcriptions']
    assert state == (
        'Transcription failed: Gemini API error 429 &lt;RESOURCE_EXHAUSTED&gt;|≈ $0.0008|'
        '||Gemini is rate-limiting the API key, try again in a minute.|'
    )
    assert transcribe_buttons(result)[0] == {
        'text': 'Transcribe',
        'disabled': False,
        'title': None,
        'transcribe': 'Some_video-abc.mp4',
    }
    assert result['clears'] == 1


def test_completed_transcription_lists_the_transcripts(page_script):
    result = run_scenario(page_script, 'transcription-completed')

    assert result['groups'] == ['Media', 'Subtitles', 'Transcripts', 'Details']
    assert f'/downloads/{JOB_ID}/Some_video-abc.transcript.md' in result['links']
    assert (
        '|Some_video-abc.transcript.md||2.0 KB||[Download][Show][Delete Some_video-abc.transcript.md]|'
        in result['rows']
    )
    assert (
        '|Some_video-abc.transcript.json||4.0 KB||[Download][Delete Some_video-abc.transcript.json]|' in result['rows']
    )
    assert result['transcriptions'] == [
        'Transcribed by gemini-3.5-flash-lite||Warning: The transcript ends at &lt;00:04:00&gt;.|'
    ]
    assert transcribe_buttons(result)[0]['text'] == 'Transcribe again'
    assert result['clears'] == 1


def test_transcribe_posts_with_the_csrf_token_and_polls_until_it_ends(page_script):
    result = run_scenario(page_script, 'transcribe-click')

    assert result['fetches'][1] == {
        'url': f'/downloads/{JOB_ID}/%22%3E%3Cimg%20src%3Dx%20onerror%3Dalert(1)%3E.mp4/transcribe',
        'method': 'POST',
        'body': None,
        'csrf': 'page-token',
    }
    assert result['fetches'][2]['url'] == f'/status/{JOB_ID}'
    assert result['transcriptions'] == ['||Transcription queued...']
    # The first poll found nothing running and stopped, the queued transcription started polling again
    assert (result['intervals'], result['clears']) == (2, 1)
    assert result['alerts'] == []


def test_refused_transcribe_tells_the_user(page_script):
    result = run_scenario(page_script, 'transcribe-refused')

    assert result['alerts'] == ['This file is already being transcribed.']


def test_show_renders_the_transcript_escaped(page_script):
    result = run_scenario(page_script, 'transcript-show')

    assert result['fetches'][1]['url'] == f'/downloads/{JOB_ID}/Some_video-abc.transcript.md'
    assert result['transcriptText'] == ['# Some video\n\n**[00:01] Speaker 1:** &lt;script&gt;alert(1)&lt;/script&gt;']
    assert '<script>' not in result['html']
    assert '[Download][Hide][Delete Some_video-abc.transcript.md]' in result['rows'][4]


def test_completed_transcription_shows_its_estimated_cost(page_script):
    result = run_scenario(page_script, 'transcription-costs')

    assert result['costs'] == [
        {
            'text': '≈ $0.013',
            'title': '11,293 input tokens (8,924 audio), 4,019 output tokens, 0 thinking tokens. Estimated with the list '
            'prices of gemini-3.5-flash-lite as of 2026-10-30, in USD per million tokens: 0.3 text input, 0.3 audio '
            'input, 2.5 output.',
        },
        {
            'text': '123 tokens',
            'title': 'No price is known for my-&lt;b&gt;model&lt;/b&gt;. 100 input tokens, 20 output tokens, 3 thinking tokens',
        },
    ]
    assert '<b>model' not in result['html']
