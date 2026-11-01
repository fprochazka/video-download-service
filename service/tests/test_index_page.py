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


def test_submit_of_a_link_already_downloaded_opens_the_earlier_job_with_a_note(page_script):
    result = run_scenario(page_script, 'submit-existing')

    assert result['assigned'] == [f'/jobs/{JOB_ID}?existing=1']


def test_earlier_job_says_when_it_was_downloaded_and_downloads_again_with_force(page_script):
    result = run_scenario(page_script, 'existing-completed')

    assert result['notice']['className'] == 'notice shown'
    assert result['notice']['html'].startswith('Already downloaded on ')
    assert 'data-download-again="1">Download again</button>' in result['notice']['html']
    assert result['fetches'][-1] == {
        'url': '/download',
        'method': 'POST',
        'body': [['url', 'https://youtu.be/dQw4w9WgXcQ?si=<b>x</b>'], ['force', 'true']],
        'csrf': 'page-token',
    }
    assert result['assigned'] == ['/jobs/new-job']


def test_earlier_job_that_still_downloads_says_since_when(page_script):
    result = run_scenario(page_script, 'existing-downloading')

    assert result['notice']['html'].startswith('This link is already being downloaded, since ')
    assert result['status']['className'] == 'status loading'


def test_job_page_opened_directly_has_no_note(page_script):
    result = run_scenario(page_script, 'existing-absent')

    assert result['notice'] == {'className': 'notice', 'html': ''}


def test_job_page_polls_the_status_of_the_job_in_the_path(page_script):
    result = run_scenario(page_script, 'job-page')

    assert result['fetches'] == [{'url': f'/status/{JOB_ID}', 'method': 'GET', 'body': None, 'csrf': None}]
    # Polls every 5 s until the first answer says the download runs, then every 2 s
    assert result['intervalMs'] == [5000, 2000]
    assert result['clears'] == 1
    assert result['status']['className'] == 'status loading'
    assert 'Some video' in result['status']['text']
    assert result['submitDisabled'] is False


EVIL_TITLE = 'Some &lt;b&gt;video&lt;/b&gt;'


def test_downloading_job_shows_the_video_and_the_progress_of_its_part(page_script):
    result = run_scenario(page_script, 'job-downloading')

    assert result['status']['className'] == 'status loading'
    assert result['thumbnails'] == [
        'class="job-thumb" src="https://i.example.com/vi/abc/hq.jpg?a=1&amp;b=&quot;&gt;&lt;script&gt;alert(1)&lt;/script&gt;" '
        'alt="" loading="lazy" referrerpolicy="no-referrer"'
    ]
    assert result['status']['text'] == (
        f'Downloading the video (1/2)...{EVIL_TITLE}jawed &lt;i&gt; · 1:02:05 · 1920x1080 · ≈ 50.0 MB'
        '25% · 12.3 MB of 50.0 MB · 2.1 MB/s · 0:18 left'
    )
    assert result['progressWidths'] == ['24.6%']
    assert '<script>' not in result['html']
    assert '<b>video' not in result['html']


def test_downloading_job_without_a_total_size_shows_what_it_downloaded(page_script):
    result = run_scenario(page_script, 'job-downloading-no-total')

    assert result['progressWidths'] == []
    assert result['progressText'] == ['3.0 MB downloaded · fragment 7/120']


def test_downloading_job_marks_an_estimated_total(page_script):
    result = run_scenario(page_script, 'job-downloading-estimate')

    assert result['progressWidths'] == ['10%']
    assert result['progressText'] == ['10% · 1.0 MB of ≈ 10.0 MB · 1.0 KB/s · 1:01:40 left']


def test_merging_job_says_so_without_a_progress_bar(page_script):
    result = run_scenario(page_script, 'job-merging')

    assert result['status']['text'].startswith(f'Merging video and audio...{EVIL_TITLE}')
    assert (result['progressWidths'], result['progressText']) == ([], [])


def test_extracted_job_shows_the_video_before_the_download_reports_progress(page_script):
    result = run_scenario(page_script, 'job-extracted')

    assert result['status']['text'].startswith(f'Starting the download...{EVIL_TITLE}')
    assert len(result['thumbnails']) == 1


def test_thumbnail_that_is_not_an_http_url_is_left_out(page_script):
    result = run_scenario(page_script, 'job-unsafe-thumbnail')

    assert result['thumbnails'] == []
    assert 'javascript:' not in result['html']


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


UNAVAILABLE = 'Transcription is not configured on the server (TRANSCRIPTION_PROVIDER, see README)'
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
        '||The transcription provider is rate-limiting the API key, try again in a minute.|'
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


def test_a_transcription_priced_per_hour_shows_the_length_of_its_audio(page_script):
    result = run_scenario(page_script, 'transcription-costs-per-hour')

    assert result['costs'] == [
        {
            'text': '≈ $0.022',
            'title': '357 s of audio. Estimated with the list price of scribe_v2 as of 2026-11-01: 0.22 USD per hour of '
            'audio.',
        },
        {'text': '357 s of audio', 'title': 'No price is known for scribe_&lt;b&gt;v9&lt;/b&gt;. 357 s of audio'},
    ]
    assert '<b>v9' not in result['html']


def test_a_cost_the_provider_reported_is_shown_without_the_approximation_sign(page_script):
    result = run_scenario(page_script, 'transcription-costs-reported')

    assert result['costs'] == [
        {
            'text': '$0.000364',
            'title': 'The cost ElevenLabs reported for the request, 1 credit. The estimate from the list price was '
            '≈ $0.0004.',
        },
        {'text': '$1.50', 'title': 'The cost ElevenLabs reported for the request.'},
    ]
