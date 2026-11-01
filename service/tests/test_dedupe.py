import json
import threading
from datetime import UTC, datetime, timedelta

import jsonschema
import pytest
from fastapi.testclient import TestClient
from fastmcp import Client
from fastmcp_tasks import call_tool_task

from tests.test_mcp import mcp_client, running_app
from tests.ytdlp_fakes import FakeYoutubeDL, raise_like_ytdlp, read_metadata, write_media
from video_download import cleanup, config, download_index, jobs, storage, urls, web

VIDEO_ID = 'dQw4w9WgXcQ'
VIDEO_URL = f'https://www.youtube.com/watch?v={VIDEO_ID}'
MEDIA = f'Never_Gonna-{VIDEO_ID}.mp4'


def video(opts, url):
    return {'id': VIDEO_ID, 'title': 'Never Gonna', 'requested_downloads': [{'filepath': write_media(opts, MEDIA)}]}


def key_of(url: str) -> str:
    return urls.video_key(*urls.resolve_download_url(url))


@pytest.mark.parametrize(
    ('url', 'key'),
    [
        (VIDEO_URL, f'Youtube:{VIDEO_ID}'),
        (f'https://youtu.be/{VIDEO_ID}?si=abc', f'Youtube:{VIDEO_ID}'),
        (f'{VIDEO_URL}&t=42s', f'Youtube:{VIDEO_ID}'),
        (f'https://m.youtube.com/watch?v={VIDEO_ID}', f'Youtube:{VIDEO_ID}'),
        (f'https://www.youtube.com/shorts/{VIDEO_ID}', f'Youtube:{VIDEO_ID}'),
        (f'https://music.youtube.com/watch?v={VIDEO_ID}&list=RDAMVM{VIDEO_ID}', f'Youtube:{VIDEO_ID}'),
        (f'{VIDEO_URL}&list=PLx0sYbCqOb8TBPRdmBHs5Iftvv9TPboYG&index=3', f'Youtube:{VIDEO_ID}'),
        ('https://www.youtube.com/watch?v=jNQXAC9IVRw', 'Youtube:jNQXAC9IVRw'),
        ('https://www.instagram.com/p/C1abcDEF/', 'Instagram:C1abcDEF'),
        ('https://www.instagram.com/reel/C1abcDEF/?igsh=xyz', 'Instagram:C1abcDEF'),
        ('https://www.instagram.com/reels/C1abcDEF/', 'Instagram:C1abcDEF'),
        ('https://www.instagram.com/p/C1abcDEg/', 'Instagram:C1abcDEg'),
        ('https://www.tiktok.com/@user/video/7300000000000000000?is_from_webapp=1', 'TikTok:7300000000000000000'),
        ('https://vimeo.com/123456789', 'Vimeo:123456789'),
        ('https://player.vimeo.com/video/123456789', 'Vimeo:123456789'),
        (
            'HTTPS://Example.COM/Some/Video.mp4?b=2&utm_source=x&fbclid=y&a=1#t=10',
            'url:https://example.com/Some/Video.mp4?b=2&a=1',
        ),
        ('https://example.com/page?si=1&feature=share&gclid=z', 'url:https://example.com/page'),
    ],
)
def test_video_key_comes_from_the_link_alone(url, key):
    assert key_of(url) == key


def submit(client, url, **data):
    response = client.post('/download', data={'url': url, **data})
    assert response.status_code == 200
    return response.json()


def test_a_link_already_downloaded_answers_with_the_earlier_job(client, downloads_dir):
    FakeYoutubeDL.behaviour = video
    first = submit(client, VIDEO_URL)

    again = submit(client, f'https://youtu.be/{VIDEO_ID}?t=42')

    assert first['existing'] is False
    assert again == {'download_id': first['download_id'], 'existing': True}
    assert len(FakeYoutubeDL.instances) == 1
    assert [p.name for p in downloads_dir.iterdir() if p.is_dir()] == [first['download_id']]


def test_force_downloads_a_link_again(client, downloads_dir):
    FakeYoutubeDL.behaviour = video
    first = submit(client, VIDEO_URL)

    forced = submit(client, VIDEO_URL, force='true')

    assert forced['existing'] is False
    assert forced['download_id'] != first['download_id']
    assert read_metadata(downloads_dir, forced['download_id'])['status'] == 'completed'
    # The newest job of the video is the one a next submit finds
    assert submit(client, VIDEO_URL) == {'download_id': forced['download_id'], 'existing': True}


def test_a_job_whose_media_was_deleted_does_not_count(client, downloads_dir):
    FakeYoutubeDL.behaviour = video
    first = submit(client, VIDEO_URL)
    assert client.delete(f'/downloads/{first["download_id"]}/{MEDIA}').status_code == 200

    again = submit(client, VIDEO_URL)

    assert again['existing'] is False
    assert again['download_id'] != first['download_id']


def test_a_job_whose_media_the_gc_deleted_does_not_count(client, downloads_dir):
    FakeYoutubeDL.behaviour = lambda opts, url: (
        video(opts, url) | {'requested_downloads': [{'filepath': write_media(opts, MEDIA, b'x' * 2048)}]}
    )
    first = submit(client, VIDEO_URL)
    settings = config.GcSettings(
        enabled=True, dry_run=False, max_age=timedelta(days=1), min_size_bytes=1024, interval_seconds=3600
    )
    index_before = download_index.index_path(downloads_dir).read_bytes()

    cleanup.collect_garbage(downloads_dir, datetime.now(UTC) + timedelta(days=2), settings)

    assert read_metadata(downloads_dir, first['download_id'])['deleted_files'][0]['reason'] == 'gc'
    assert download_index.index_path(downloads_dir).read_bytes() == index_before
    assert submit(client, VIDEO_URL)['existing'] is False


def test_a_failed_job_does_not_count(client, downloads_dir):
    FakeYoutubeDL.behaviour = lambda opts, url: raise_like_ytdlp(Exception('Video unavailable'))
    first = submit(client, VIDEO_URL)
    FakeYoutubeDL.behaviour = video

    again = submit(client, VIDEO_URL)

    assert read_metadata(downloads_dir, first['download_id'])['status'] == 'failed'
    assert again['existing'] is False
    assert read_metadata(downloads_dir, again['download_id'])['status'] == 'completed'


class RecordingWorker:
    """Queues nothing, so the jobs it gets stay pending"""

    def __init__(self):
        self.enqueued = []
        self.lock = threading.Lock()

    def enqueue(self, lane, job, *args):
        with self.lock:
            self.enqueued.append(args[0])


def test_a_download_that_still_runs_is_the_existing_job(downloads_dir):
    downloads_dir.mkdir()
    worker = RecordingWorker()
    first = jobs.submit_download(worker, VIDEO_URL)
    storage.update_metadata(downloads_dir / first.download_id, status='downloading')

    again = jobs.submit_download(worker, f'https://www.youtube.com/shorts/{VIDEO_ID}')

    assert again == jobs.Submission(first.download_id, existing=True)
    assert worker.enqueued == [first.download_id]


def test_concurrent_submits_of_one_link_create_one_job(downloads_dir):
    downloads_dir.mkdir()
    worker = RecordingWorker()
    barrier = threading.Barrier(8)
    results = []

    def submit_one():
        barrier.wait()
        results.append(jobs.submit_download(worker, VIDEO_URL))

    threads = [threading.Thread(target=submit_one) for _ in range(8)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert len(worker.enqueued) == 1
    assert {result.download_id for result in results} == set(worker.enqueued)
    assert sorted(result.existing for result in results) == [False] + [True] * 7
    assert len(download_index.read_index(downloads_dir)) == 1


def legacy_job(downloads_dir, download_id, url, **fields):
    job_dir = downloads_dir / download_id
    job_dir.mkdir(parents=True)
    (job_dir / MEDIA).write_bytes(b'video')
    metadata = {
        'download_id': download_id,
        'url': url,
        'status': 'completed',
        'files': [MEDIA],
        'created_at': '2025-01-14T12:00:00',
    }
    storage.save_metadata(job_dir, metadata | fields)


def test_startup_indexes_the_jobs_from_their_metadata(downloads_dir):
    legacy_job(downloads_dir, '0b7f6c1e-3f4a-4d2b-9c8e-1a2b3c4d5e6f', f'https://youtu.be/{VIDEO_ID}')
    legacy_job(
        downloads_dir,
        '019a3b2c-4d5e-7f00-8a1b-2c3d4e5f6a7b',
        'https://www.instagram.com/reel/C1abcDEF/?igsh=xyz',
        download_url='https://www.instagram.com/reel/C1abcDEF/?igsh=xyz',
        extractor='Instagram',
        created_at='2026-10-01T10:00:00+00:00',
    )
    legacy_job(downloads_dir, '1c8e7d2f-4a5b-4e3c-8d9f-2b3c4d5e6f70', 'https://www.youtube.com/@SomeChannel/videos')
    (downloads_dir / 'stray-file').write_text('x')

    with TestClient(web.app):
        pass
    with TestClient(web.app):
        pass

    entries = download_index.read_index(downloads_dir)
    assert sorted((e['key'], e['download_id']) for e in entries) == [
        ('Instagram:C1abcDEF', '019a3b2c-4d5e-7f00-8a1b-2c3d4e5f6a7b'),
        (f'Youtube:{VIDEO_ID}', '0b7f6c1e-3f4a-4d2b-9c8e-1a2b3c4d5e6f'),
        ('url:https://www.youtube.com/@SomeChannel/videos', '1c8e7d2f-4a5b-4e3c-8d9f-2b3c4d5e6f70'),
    ]
    assert jobs.submit_download(RecordingWorker(), VIDEO_URL) == jobs.Submission(
        '0b7f6c1e-3f4a-4d2b-9c8e-1a2b3c4d5e6f', existing=True
    )


def test_startup_adds_only_the_jobs_the_index_lacks(downloads_dir):
    legacy_job(downloads_dir, '0b7f6c1e-3f4a-4d2b-9c8e-1a2b3c4d5e6f', VIDEO_URL)
    assert download_index.backfill_index(downloads_dir) == 1
    legacy_job(downloads_dir, '019a3b2c-4d5e-7f00-8a1b-2c3d4e5f6a7b', 'https://vimeo.com/123456789')

    assert download_index.backfill_index(downloads_dir) == 1
    assert download_index.backfill_index(downloads_dir) == 0
    assert len(download_index.read_index(downloads_dir)) == 2


def test_a_truncated_last_line_is_skipped_and_the_next_entry_starts_its_own_line(downloads_dir):
    legacy_job(downloads_dir, '0b7f6c1e-3f4a-4d2b-9c8e-1a2b3c4d5e6f', VIDEO_URL)
    download_index.backfill_index(downloads_dir)
    path = download_index.index_path(downloads_dir)
    with open(path, 'a', encoding='utf-8') as f:
        f.write('{"key": "Vimeo:1", "download_id": "019a')

    assert [e['key'] for e in download_index.read_index(downloads_dir)] == [f'Youtube:{VIDEO_ID}']

    created = jobs.submit_download(RecordingWorker(), 'https://vimeo.com/123456789')

    lines = path.read_text(encoding='utf-8').splitlines()
    assert len(lines) == 3
    assert json.loads(lines[2])['download_id'] == created.download_id
    assert [e['key'] for e in download_index.read_index(downloads_dir)] == [f'Youtube:{VIDEO_ID}', 'Vimeo:123456789']


@pytest.mark.parametrize(
    'path',
    [
        '/downloads/.index.jsonl',
        '/.index.jsonl',
        '/downloads/.index/downloads.jsonl',
        '/downloads/index.jsonl',
        '/downloads/{id}/.index.jsonl',
        '/downloads/{id}/..%2F.index.jsonl',
        '/downloads/{id}/%2e%2e%2f.index.jsonl',
        '/downloads/..%2F.index.jsonl/x',
        '/status/.index.jsonl',
        '/jobs/.index.jsonl',
    ],
)
def test_the_index_is_never_served(client, downloads_dir, path):
    FakeYoutubeDL.behaviour = video
    download_id = submit(client, VIDEO_URL)['download_id']
    assert download_index.index_path(downloads_dir).is_file()

    response = client.get(path.replace('{id}', download_id))

    assert response.status_code in (404, 422)
    assert download_id not in response.text


@pytest.mark.anyio
async def test_download_video_answers_with_the_existing_job(downloads_dir):
    FakeYoutubeDL.behaviour = video

    async with running_app(), mcp_client() as client:
        schema = {tool.name: tool for tool in await client.list_tools()}['download_video']
        first = await client.call_tool('download_video', {'url': VIDEO_URL})
        await web.app.state.worker.join()
        again = await client.call_tool('download_video', {'url': f'https://youtu.be/{VIDEO_ID}'})
        forced = await client.call_tool('download_video', {'url': VIDEO_URL, 'force': True})
        await web.app.state.worker.join()
        got = await client.call_tool('get_job', {'download_id': first.structured_content['download_id']})

    assert set(schema.input_schema['properties']) == {'url', 'force'}
    assert first.structured_content['existing'] is False
    job = again.structured_content
    assert (job['download_id'], job['existing'], job['state']) == (
        first.structured_content['download_id'],
        True,
        'completed',
    )
    assert job['files'][0]['name'] == MEDIA
    assert 'already downloaded' in again.content[0].text
    assert 'force=true' in again.content[0].text
    assert forced.structured_content['download_id'] != job['download_id']
    assert forced.structured_content['existing'] is False
    assert got.structured_content['existing'] is False
    for result in (first, again, forced):
        jsonschema.validate(result.structured_content, schema.output_schema)
    assert len(FakeYoutubeDL.instances) == 2


@pytest.mark.anyio
async def test_a_tasks_client_gets_a_completed_task_for_an_existing_job(downloads_dir):
    FakeYoutubeDL.behaviour = video

    async with running_app(), mcp_client(client_class=Client) as client:
        first = await call_tool_task(client, 'download_video', {'url': VIDEO_URL})
        await web.app.state.worker.join()
        again = await call_tool_task(client, 'download_video', {'url': VIDEO_URL})
        result = await again.result()

    assert again.task_id == first.task_id
    assert again.create_result.status == 'completed'
    assert result.structured_content['state'] == 'completed'
    assert len(FakeYoutubeDL.instances) == 1
