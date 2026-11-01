import json
import logging
from pathlib import Path

import anyio
import jsonschema
import pytest

from tests.test_mcp import VIDEO_URL, mcp_client, running_app
from tests.test_mcp_tasks import get_task, tasks_client
from tests.ytdlp_fakes import (
    FakeYoutubeDL,
    raise_like_ytdlp,
    read_metadata,
    report_postprocessing,
    report_progress,
    write_media,
)
from video_download import mcp_server, progress, storage

JOB_ID = '019a3b2c-4d5e-7f00-8a1b-2c3d4e5f6a7b'
MEDIA = 'Me_at_the_zoo-jNQXAC9IVRw.mp4'
VIDEO_PART = {'format_id': '137', 'vcodec': 'avc1.640028', 'acodec': 'none', 'ext': 'mp4', 'filesize': 40_000_000}
AUDIO_PART = {'format_id': '140', 'vcodec': 'none', 'acodec': 'mp4a.40.2', 'ext': 'm4a', 'filesize_approx': 3_000_000}

DOWNLOADING_VIDEO = {
    'phase': 'downloading',
    'part': 'video',
    'part_index': 1,
    'part_count': 2,
    'downloaded_bytes': 16_800_000,
    'total_bytes': 40_000_000,
    'total_estimated': False,
    'percent': 42.0,
    'speed': 2_000_000.0,
    'eta': 12,
    'fragment_index': None,
    'fragment_count': None,
    'filename': 'Me_at_the_zoo-jNQXAC9IVRw.f137.mp4',
    'updated_at': '2026-11-01T10:00:05+00:00',
}


def extracted(opts, url):
    """The info of a video yt-dlp downloads as separate video and audio formats, merged afterwards"""
    return {
        'id': 'jNQXAC9IVRw',
        'title': 'Me at the zoo',
        'duration': 19,
        'uploader': 'jawed',
        'channel': 'jawed',
        'upload_date': '20050424',
        'thumbnail': 'https://i.ytimg.com/vi/jNQXAC9IVRw/hqdefault.jpg',
        'webpage_url': VIDEO_URL,
        'description': 'The first video on YouTube.',
        'resolution': '1920x1080',
        'format': '137+140',
        'requested_formats': [VIDEO_PART, AUDIO_PART],
        'chapters': [{'start_time': 0, 'title': 'Elephants'}, {'start_time': 10, 'title': 'Trunks'}],
        'subtitles': {'en': [{'ext': 'vtt', 'url': 'https://example.com/en.vtt'}]},
    }


def part_info(part: dict) -> dict:
    return {'title': 'Me at the zoo', 'id': 'jNQXAC9IVRw'} | part


def downloading(part: dict, downloaded: int, **status) -> dict:
    total = part.get('filesize')
    filename = f'/downloads/x/Me_at_the_zoo-jNQXAC9IVRw.f{part["format_id"]}.{part["ext"]}'
    return {
        'status': 'downloading',
        'downloaded_bytes': downloaded,
        'total_bytes': total,
        'filename': filename,
    } | status


def finish_download(ydl, info):
    info['requested_downloads'] = [{'filepath': write_media(ydl.params, MEDIA)}]


def write_job(downloads_dir, **fields):
    (downloads_dir / JOB_ID).mkdir(parents=True, exist_ok=True)
    metadata = {'download_id': JOB_ID, 'url': VIDEO_URL, 'status': 'downloading', 'files': [], 'title': 'Me at the zoo'}
    metadata |= {'created_at': '2026-11-01T10:00:00+00:00', 'updated_at': '2026-11-01T10:00:00+00:00'} | fields
    (downloads_dir / JOB_ID / 'metadata.json').write_text(json.dumps(metadata))


def test_the_details_are_stored_before_the_download_and_the_progress_while_it_runs(client, downloads_dir, monkeypatch):
    # Every write below is a phase or part change, a forced write, or the first one
    monkeypatch.setattr(progress, 'WRITE_INTERVAL_SECONDS', 3600)
    snapshots = []

    def download(ydl, info):
        download_dir = Path(ydl.params['paths']['home'])
        snapshots.append(read_metadata(downloads_dir, download_dir.name))
        report_progress(ydl, part_info(VIDEO_PART), **downloading(VIDEO_PART, 1_000_000, speed=500_000.0, eta=78))
        report_progress(ydl, part_info(VIDEO_PART), **downloading(VIDEO_PART, 16_800_000, speed=2e6, eta=12))
        snapshots.append(read_metadata(downloads_dir, download_dir.name))
        report_progress(ydl, part_info(VIDEO_PART), **downloading(VIDEO_PART, 40_000_000, status='finished'))
        snapshots.append(read_metadata(downloads_dir, download_dir.name))
        audio = downloading(AUDIO_PART, 1_000_000, total_bytes=None, total_bytes_estimate=3_000_000)
        report_progress(ydl, part_info(AUDIO_PART), **audio)
        snapshots.append(read_metadata(downloads_dir, download_dir.name))
        report_postprocessing(ydl, {'filepath': f'/downloads/x/{MEDIA}'}, 'Merger')
        snapshots.append(read_metadata(downloads_dir, download_dir.name))
        finish_download(ydl, info)

    FakeYoutubeDL.behaviour = extracted
    FakeYoutubeDL.download_behaviour = download

    download_id = client.post('/download', data={'url': VIDEO_URL}).json()['download_id']

    before, first, video_done, audio, merging = snapshots
    assert before['status'] == 'downloading'
    assert 'progress' not in before
    assert (before['title'], before['uploader'], before['duration']) == ('Me at the zoo', 'jawed', 19)
    assert before['thumbnail'] == 'https://i.ytimg.com/vi/jNQXAC9IVRw/hqdefault.jpg'
    assert (before['upload_date'], before['webpage_url'], before['resolution']) == ('20050424', VIDEO_URL, '1920x1080')
    assert before['chapters_count'] == 2
    assert before['filesize_approx'] == 43_000_000
    assert before['expected_subtitles'] == [{'lang': 'en', 'auto': False}]
    # The second report came within the write interval and is not written
    assert first['progress'] | {'updated_at': None} == {
        'phase': 'downloading',
        'part': 'video',
        'part_index': 1,
        'part_count': 2,
        'downloaded_bytes': 1_000_000,
        'total_bytes': 40_000_000,
        'total_estimated': False,
        'percent': 2.5,
        'speed': 500_000.0,
        'eta': 78,
        'fragment_index': None,
        'fragment_count': None,
        'filename': 'Me_at_the_zoo-jNQXAC9IVRw.f137.mp4',
        'updated_at': None,
    }
    assert (video_done['progress']['percent'], video_done['progress']['downloaded_bytes']) == (100.0, 40_000_000)
    assert (audio['progress']['part'], audio['progress']['part_index'], audio['progress']['percent']) == (
        'audio',
        2,
        33.3,
    )
    assert (audio['progress']['total_bytes'], audio['progress']['total_estimated']) == (3_000_000, True)
    assert merging['progress'] | {'updated_at': None} == {'phase': 'merging', 'filename': MEDIA, 'updated_at': None}
    final = read_metadata(downloads_dir, download_id)
    assert final['status'] == 'completed'
    assert final['progress'] is None
    assert client.get(f'/status/{download_id}').json()['progress'] is None


def test_progress_is_written_at_most_every_interval_and_at_each_part_change(downloads_dir):
    write_job(downloads_dir)
    now = [0.0]
    reporter = progress.DownloadProgress(downloads_dir / JOB_ID, clock=lambda: now[0])
    reporter.extracted({'title': 'Me at the zoo', 'requested_formats': [VIDEO_PART, AUDIO_PART]}, {})
    written = []

    def report(at: float, part: dict, downloaded: int, **status):
        now[0] = at
        reporter.progress_hook(downloading(part, downloaded, **status) | {'info_dict': part_info(part)})
        written.append((read_metadata(downloads_dir, JOB_ID).get('progress') or {}).get('downloaded_bytes'))

    report(0.0, VIDEO_PART, 1)
    report(1.0, VIDEO_PART, 2)
    report(1.6, VIDEO_PART, 3)
    report(1.7, VIDEO_PART, 4, status='finished')
    report(1.8, AUDIO_PART, 5)
    report(1.9, AUDIO_PART, 6)

    assert written == [1, 1, 3, 4, 5, 5]


def test_subtitles_and_postprocessing_have_their_own_phase(downloads_dir):
    write_job(downloads_dir)
    reporter = progress.DownloadProgress(downloads_dir / JOB_ID)
    reporter.extracted({}, {})
    phases = []

    def phase():
        phases.append(read_metadata(downloads_dir, JOB_ID)['progress']['phase'])

    reporter.progress_hook(downloading(VIDEO_PART, 1) | {'info_dict': part_info(VIDEO_PART)})
    phase()
    reporter.postprocessor_hook({'status': 'started', 'postprocessor': 'FixupM3u8', 'info_dict': {}})
    phase()
    reporter.start_subtitles()
    reporter.progress_hook({'status': 'downloading', 'downloaded_bytes': 10, 'info_dict': {}})
    phase()
    # A subtitle conversion is not postprocessing of the media
    reporter.postprocessor_hook({'status': 'started', 'postprocessor': 'FFmpegSubtitlesConvertor', 'info_dict': {}})
    phase()

    assert phases == ['downloading', 'postprocessing', 'subtitles', 'subtitles']
    # One part only, no part numbers
    assert read_metadata(downloads_dir, JOB_ID)['progress']['part'] is None


def test_a_failing_hook_never_raises_and_logs_once(downloads_dir, caplog):
    reporter = progress.DownloadProgress(downloads_dir / 'missing-job')

    with caplog.at_level(logging.ERROR, logger='video_download.progress'):
        reporter.extracted({'title': 'x', 'chapters': 'not a list'}, {})
        reporter.progress_hook({'status': 'downloading', 'downloaded_bytes': 1, 'info_dict': {}})
        reporter.postprocessor_hook({'status': 'started', 'postprocessor': 'Merger', 'info_dict': {}})

    assert [r.message for r in caplog.records] == ['Could not record the progress of download missing-job']


def test_a_closed_reporter_writes_nothing(downloads_dir):
    write_job(downloads_dir, status='failed')
    reporter = progress.DownloadProgress(downloads_dir / JOB_ID)
    reporter.close()

    reporter.progress_hook(downloading(VIDEO_PART, 1) | {'info_dict': part_info(VIDEO_PART)})

    assert 'progress' not in read_metadata(downloads_dir, JOB_ID)


def test_a_broken_progress_report_does_not_fail_the_download(client, downloads_dir):
    def download(ydl, info):
        report_progress(ydl, ['not', 'an', 'info', 'dict'], status='downloading', downloaded_bytes=float('nan'))
        finish_download(ydl, info)

    FakeYoutubeDL.behaviour = extracted
    FakeYoutubeDL.download_behaviour = download

    download_id = client.post('/download', data={'url': VIDEO_URL}).json()['download_id']

    assert read_metadata(downloads_dir, download_id)['status'] == 'completed'


def test_a_failed_download_clears_its_progress(client, downloads_dir):
    def download(ydl, info):
        report_progress(ydl, part_info(VIDEO_PART), **downloading(VIDEO_PART, 1_000_000))
        raise_like_ytdlp(Exception('Connection reset by peer'))

    FakeYoutubeDL.behaviour = extracted
    FakeYoutubeDL.download_behaviour = download

    download_id = client.post('/download', data={'url': VIDEO_URL}).json()['download_id']

    metadata = read_metadata(downloads_dir, download_id)
    assert metadata['status'] == 'failed'
    assert metadata['progress'] is None
    assert metadata['title'] == 'Me at the zoo'


def test_status_returns_the_details_and_the_progress_of_a_running_download(client, downloads_dir):
    write_job(downloads_dir, thumbnail='https://i.ytimg.com/x.jpg', chapters_count=2, progress=DOWNLOADING_VIDEO)

    status = client.get(f'/status/{JOB_ID}').json()

    assert status['progress'] == DOWNLOADING_VIDEO
    assert (status['thumbnail'], status['chapters_count'], status['title']) == (
        'https://i.ytimg.com/x.jpg',
        2,
        'Me at the zoo',
    )


@pytest.mark.parametrize(
    ('job_progress', 'message'),
    [
        (None, 'downloading'),
        ({'phase': 'downloading', 'percent': 7.4}, 'downloading 7%'),
        (DOWNLOADING_VIDEO, 'downloading 42% (video 1/2)'),
        ({'phase': 'downloading', 'downloaded_bytes': 5}, 'downloading'),
        ({'phase': 'merging'}, 'downloading, merging'),
        ({'phase': 'subtitles', 'percent': 100.0}, 'downloading subtitles 100%'),
    ],
)
def test_job_state_message_has_the_percent_of_the_download(job_progress, message):
    assert mcp_server.job_state({'status': 'downloading', 'progress': job_progress}) == ('downloading', message)


@pytest.mark.anyio
async def test_get_job_and_the_task_carry_the_progress(downloads_dir):
    async with running_app():
        write_job(
            downloads_dir, thumbnail='https://i.ytimg.com/x.jpg', resolution='1920x1080', progress=DOWNLOADING_VIDEO
        )
        async with mcp_client() as client:
            schema = {tool.name: tool.output_schema for tool in await client.list_tools()}['get_job']
            result = await client.call_tool('get_job', {'download_id': JOB_ID})
        async with tasks_client() as client:
            task = await get_task(client, JOB_ID)

    job = result.structured_content
    jsonschema.validate(job, schema)
    assert job['progress'] == DOWNLOADING_VIDEO
    assert (job['thumbnail'], job['resolution'], job['message']) == (
        'https://i.ytimg.com/x.jpg',
        '1920x1080',
        'downloading 42% (video 1/2)',
    )
    assert f'Job {JOB_ID}: downloading 42% (video 1/2).' in result.content[0].text
    assert task.status_message == 'downloading 42% (video 1/2)'


@pytest.mark.anyio
async def test_a_progress_write_does_not_end_the_wait_of_get_job(downloads_dir, monkeypatch):
    monkeypatch.setattr(mcp_server, 'WAIT_POLL_SECONDS', 0.01)
    write_job(downloads_dir)
    download_dir = downloads_dir / JOB_ID
    metadata = storage.load_metadata(download_dir)

    async def write_progress_then_title():
        await anyio.sleep(0.05)
        storage.update_metadata(download_dir, progress={'phase': 'downloading', 'percent': 1.0})
        await anyio.sleep(0.1)
        storage.update_metadata(download_dir, title='Me at the zoo, renamed')

    async with anyio.create_task_group() as group:
        group.start_soon(write_progress_then_title)
        changed = await mcp_server.wait_for_change(download_dir, metadata, 5)

    assert changed['title'] == 'Me at the zoo, renamed'
