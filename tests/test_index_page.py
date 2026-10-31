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
        {'url': '/download', 'method': 'POST', 'body': [['url', 'https://www.youtube.com/watch?v=dQw4w9WgXcQ']]}
    ]
    assert result['assigned'] == [f'/jobs/{JOB_ID}']


def test_job_page_polls_the_status_of_the_job_in_the_path(page_script):
    result = run_scenario(page_script, 'job-page')

    assert result['fetches'] == [{'url': f'/status/{JOB_ID}', 'method': 'GET', 'body': None}]
    assert result['intervals'] == 1
    assert result['status']['className'] == 'status loading'
    assert 'Some video' in result['status']['text']
    assert result['submitDisabled'] is False


def test_job_page_of_unknown_job_says_not_found(page_script):
    result = run_scenario(page_script, 'job-not-found')

    assert result['status'] == {'className': 'status error', 'text': 'Error: Download not found.'}


def test_completed_job_links_every_artifact(page_script):
    result = run_scenario(page_script, 'job-completed')

    assert result['status']['className'] == 'status success'
    assert result['links'] == [
        f'/downloads/{JOB_ID}/Some_video-abc.mp4',
        f'/downloads/{JOB_ID}/Some_video-abc.en.vtt',
        f'/downloads/{JOB_ID}/Some_video-abc.cs-orig.vtt',
        f'/downloads/{JOB_ID}/info.json',
    ]
    assert '<b>video</b>' not in result['html']
