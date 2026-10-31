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
        '|Some_video-abc.mp4||50.0 MB||[Download][Delete Some_video-abc.mp4]|',
        f'|{evil}||3 B||[Download][Delete {evil}]|',
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
