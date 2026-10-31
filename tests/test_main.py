import io
import json
import uuid
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from yt_dlp.networking.exceptions import TransportError
from yt_dlp.utils import ExtractorError, PostProcessingError

from tests.ytdlp_fakes import (
    FakeYoutubeDL,
    extractor_error_caused_by,
    raise_like_ytdlp,
    read_metadata,
    write_media,
)
from video_download import config, errors, storage, urls, web, ytdlp

VIDEO_URL = 'https://www.youtube.com/watch?v=dQw4w9WgXcQ'
PLAYLIST_REJECTED = 'Playlists and channels are not supported, paste a link to a single video.'


@pytest.mark.parametrize(
    ('url', 'expected'),
    [
        ('https://www.youtube.com/watch?v=dQw4w9WgXcQ&list=RDdQw4w9WgXcQ&start_radio=1', VIDEO_URL),
        ('https://www.youtube.com/watch?v=dQw4w9WgXcQ&list=PLx0sYbCqOb8TBPRdmBHs5Iftvv9TPboYG&index=3', VIDEO_URL),
        ('https://youtu.be/dQw4w9WgXcQ?list=PLx0sYbCqOb8TBPRdmBHs5Iftvv9TPboYG', 'https://youtu.be/dQw4w9WgXcQ'),
        (
            'https://music.youtube.com/watch?v=dQw4w9WgXcQ&list=RDAMVMdQw4w9WgXcQ',
            'https://music.youtube.com/watch?v=dQw4w9WgXcQ',
        ),
        (VIDEO_URL, VIDEO_URL),
        ('https://www.youtube.com/watch?v=dQw4w9WgXcQ&t=42s', 'https://www.youtube.com/watch?v=dQw4w9WgXcQ&t=42s'),
        ('https://www.youtube.com/shorts/abcdefghijk', 'https://www.youtube.com/shorts/abcdefghijk'),
        ('https://m.youtube.com/watch?v=dQw4w9WgXcQ', 'https://m.youtube.com/watch?v=dQw4w9WgXcQ'),
        (
            'https://www.youtube.com/clip/UgkxU2HSeGL_NvmDJ-nQJrlLwllwMDBdGZFs',
            'https://www.youtube.com/clip/UgkxU2HSeGL_NvmDJ-nQJrlLwllwMDBdGZFs',
        ),
        ('https://www.youtube.com/playlist?list=PLx0sYbCqOb8TBPRdmBHs5Iftvv9TPboYG', None),
        ('https://www.youtube.com/watch?list=PLx0sYbCqOb8TBPRdmBHs5Iftvv9TPboYG', None),
        ('https://music.youtube.com/playlist?list=OLAK5uy_abc', None),
        ('https://www.youtube.com/@SomeChannel/videos', None),
        ('https://www.instagram.com/p/ABC123/', 'https://www.instagram.com/p/ABC123/'),
        (
            'https://www.instagram.com/p/ABC123/?img_index=2&list=keep',
            'https://www.instagram.com/p/ABC123/?img_index=2&list=keep',
        ),
    ],
)
def test_resolve_download_url(url, expected):
    if expected is None:
        with pytest.raises(urls.InvalidDownloadUrl, match=PLAYLIST_REJECTED):
            urls.resolve_download_url(url)
    else:
        assert urls.resolve_download_url(url)[0] == expected


@pytest.mark.parametrize(
    'url',
    [
        'https://www.youtube.com/playlist?list=PLx0sYbCqOb8TBPRdmBHs5Iftvv9TPboYG',
        '',
        '   ',
        'ftp://example.com/a.mp4',
        'not a url',
    ],
)
def test_download_rejects_url_without_creating_a_job(client, downloads_dir, url):
    response = client.post('/download', data={'url': url})

    assert response.status_code in (400, 422)
    if url == 'https://www.youtube.com/playlist?list=PLx0sYbCqOb8TBPRdmBHs5Iftvv9TPboYG':
        assert response.json() == {'detail': PLAYLIST_REJECTED}
    assert list(downloads_dir.iterdir()) == []
    assert FakeYoutubeDL.instances == []


def test_successful_download_lists_only_media_files(client, downloads_dir):
    def behaviour(opts, url):
        path = write_media(opts, 'Never_Gonna-dQw4w9WgXcQ.mp4')
        part = write_media(opts, 'Never_Gonna-dQw4w9WgXcQ.f137.mp4.part')
        return {
            'id': 'dQw4w9WgXcQ',
            'title': 'Never Gonna',
            'duration': 212.4,
            'requested_downloads': [{'filepath': path}, {'filepath': part}],
        }

    FakeYoutubeDL.behaviour = behaviour

    response = client.post('/download', data={'url': VIDEO_URL + '&list=RDdQw4w9WgXcQ&start_radio=1'})

    assert response.status_code == 200
    download_id = response.json()['download_id']
    metadata = read_metadata(downloads_dir, download_id)
    assert metadata['status'] == 'completed'
    assert metadata['files'] == ['Never_Gonna-dQw4w9WgXcQ.mp4']
    assert metadata['url'] == VIDEO_URL + '&list=RDdQw4w9WgXcQ&start_radio=1'
    assert metadata['download_url'] == VIDEO_URL
    assert metadata['duration'] == 212
    assert metadata['error'] is None

    (ydl,) = FakeYoutubeDL.instances
    assert ydl.calls == [(VIDEO_URL, False)]

    status = client.get(f'/status/{download_id}').json()
    assert status['status'] == 'completed'
    assert status['download_url'] == VIDEO_URL

    file_response = client.get(f'/downloads/{download_id}/Never_Gonna-dQw4w9WgXcQ.mp4')
    assert file_response.status_code == 200
    assert file_response.content == b'video'

    ranged = client.get(f'/downloads/{download_id}/Never_Gonna-dQw4w9WgXcQ.mp4', headers={'Range': 'bytes=1-2'})
    assert ranged.status_code == 206
    assert ranged.content == b'id'


def test_playlist_result_collects_files_of_all_entries(client, downloads_dir):
    def behaviour(opts, url):
        return {
            '_type': 'playlist',
            'id': 'ABC123',
            'title': 'Carousel',
            'entries': [
                {'id': 'a', 'requested_downloads': [{'filepath': write_media(opts, 'Post-a.mp4')}]},
                {'id': 'b', 'requested_downloads': [{'filepath': write_media(opts, 'Post-b.jpg')}]},
                {'id': 'c', 'requested_downloads': [{'filepath': write_media(opts, 'Post-c.mp4', b'')}]},
            ],
        }

    FakeYoutubeDL.behaviour = behaviour

    download_id = client.post('/download', data={'url': 'https://www.instagram.com/p/ABC123/'}).json()['download_id']

    metadata = read_metadata(downloads_dir, download_id)
    assert metadata['status'] == 'completed'
    assert metadata['files'] == ['Post-a.mp4', 'Post-b.jpg']


def test_service_owned_options_win_over_user_config(client, downloads_dir, monkeypatch):
    user_opts = {
        'ignoreerrors': 'only_download',
        'noplaylist': False,
        'outtmpl': '/tmp/x',
        'format': 'worst',
        'retries': 3,
    }
    monkeypatch.setattr(config, 'load_user_config', lambda: ({'somesite': {'skip': ['hls']}}, user_opts))
    FakeYoutubeDL.behaviour = lambda opts, url: None

    download_id = client.post('/download', data={'url': VIDEO_URL}).json()['download_id']

    (ydl,) = FakeYoutubeDL.instances
    assert ydl.opts['ignoreerrors'] is False
    assert ydl.opts['noplaylist'] is True
    assert ydl.opts['outtmpl'] == ytdlp.OUTPUT_TEMPLATE
    assert ydl.opts['paths'] == {'home': str(downloads_dir / download_id)}
    assert ydl.opts['format'] == 'worst'
    assert ydl.opts['retries'] == 3
    assert ydl.opts['extractor_args'] == {'somesite': {'skip': ['hls']}}


def test_empty_result_is_failed(client, downloads_dir):
    FakeYoutubeDL.behaviour = lambda opts, url: None

    download_id = client.post('/download', data={'url': VIDEO_URL}).json()['download_id']

    metadata = read_metadata(downloads_dir, download_id)
    assert metadata['status'] == 'failed'
    assert metadata['files'] == []
    assert metadata['error_code'] == 'unknown'


def rate_limited():
    raise_like_ytdlp(
        ExtractorError(
            "This content isn't available, try again later. The current session has been rate-limited by YouTube "
            'for up to an hour.',
            expected=True,
            ie='youtube',
            video_id='dQw4w9WgXcQ',
        )
    )


def age_gated():
    raise_like_ytdlp(
        ExtractorError(
            'Sign in to confirm your age. This video may be inappropriate for some users. Use --cookies-from-browser',
            expected=True,
            ie='youtube',
            video_id='dQw4w9WgXcQ',
        )
    )


def instagram_restricted():
    raise_like_ytdlp(
        ExtractorError(
            "This content may be inappropriate: It's unavailable for certain audiences.",
            expected=True,
            ie='Instagram',
            video_id='ABC123',
        )
    )


def network_down():
    raise_like_ytdlp(
        extractor_error_caused_by(
            TransportError('Connection refused'), 'Unable to download webpage: Connection refused'
        )
    )


def postprocessing_failed():
    raise_like_ytdlp(PostProcessingError('Postprocessing: Conversion failed!'))


def crashed():
    raise RuntimeError('boom')


@pytest.mark.parametrize(
    ('raiser', 'error_code', 'error_type', 'error_start'),
    [
        (rate_limited, 'rate_limited', 'ExtractorError', "[youtube] dQw4w9WgXcQ: This content isn't available"),
        (age_gated, 'needs_auth', 'ExtractorError', '[youtube] dQw4w9WgXcQ: Sign in to confirm your age'),
        (instagram_restricted, 'needs_auth', 'ExtractorError', '[Instagram] ABC123: This content may be'),
        (network_down, 'network', 'TransportError', 'Unable to download webpage'),
        (postprocessing_failed, 'postprocessing', 'PostProcessingError', 'Postprocessing: Conversion failed!'),
        (crashed, 'unknown', 'RuntimeError', 'boom'),
    ],
)
def test_failed_download_is_classified(client, downloads_dir, caplog, raiser, error_code, error_type, error_start):
    def behaviour(opts, url):
        write_media(opts, 'leftover.mp4.part')
        raiser()

    FakeYoutubeDL.behaviour = behaviour

    download_id = client.post('/download', data={'url': VIDEO_URL}).json()['download_id']

    metadata = read_metadata(downloads_dir, download_id)
    assert metadata['status'] == 'failed'
    assert metadata['files'] == []
    assert metadata['error_code'] == error_code
    assert metadata['error_type'] == error_type
    assert metadata['error'].startswith(error_start)
    assert '\x1b' not in metadata['error']
    assert any(download_id in r.getMessage() and f'error_code={error_code}' in r.getMessage() for r in caplog.records)

    status = client.get(f'/status/{download_id}').json()
    assert status['error_code'] == error_code


def test_metadata_keeps_created_at(client, downloads_dir, monkeypatch):
    ticks = iter(f'2026-01-01T00:00:{second:02d}+00:00' for second in range(60))
    monkeypatch.setattr(storage, 'now_iso', lambda: next(ticks))
    FakeYoutubeDL.behaviour = lambda opts, url: None

    download_id = client.post('/download', data={'url': VIDEO_URL}).json()['download_id']

    metadata = read_metadata(downloads_dir, download_id)
    assert metadata['created_at'] == '2026-01-01T00:00:00+00:00'
    assert metadata['updated_at'] > metadata['created_at']


def test_startup_marks_unfinished_downloads_interrupted(downloads_dir):
    for download_id, status in [('a' * 8, 'pending'), ('b' * 8, 'downloading'), ('c' * 8, 'completed')]:
        (downloads_dir / download_id).mkdir(parents=True)
        storage.save_metadata(
            downloads_dir / download_id,
            {'download_id': download_id, 'status': status, 'url': VIDEO_URL, 'created_at': '2026-01-01T00:00:00+00:00'},
        )
    (downloads_dir / 'stray-file').write_text('x')

    with TestClient(web.app):
        pass

    for download_id in ('a' * 8, 'b' * 8):
        metadata = read_metadata(downloads_dir, download_id)
        assert metadata['status'] == 'failed'
        assert metadata['error_code'] == 'interrupted'
        assert metadata['created_at'] == '2026-01-01T00:00:00+00:00'
    assert read_metadata(downloads_dir, 'c' * 8)['status'] == 'completed'


@pytest.mark.parametrize('filename', ['download.log', 'metadata.json', 'other.mp4', '..', '%2e%2e%2fsecret'])
def test_only_listed_media_files_are_served(client, downloads_dir, filename):
    FakeYoutubeDL.behaviour = lambda opts, url: {
        'id': 'x',
        'requested_downloads': [{'filepath': write_media(opts, 'video-x.mp4')}],
    }
    download_id = client.post('/download', data={'url': VIDEO_URL}).json()['download_id']
    (downloads_dir / download_id / 'other.mp4').write_bytes(b'not listed')
    (downloads_dir / 'secret').write_text('secret')

    response = client.get(f'/downloads/{download_id}/{filename}')

    assert response.status_code == 404


def test_new_jobs_get_uuid7_ids(client):
    FakeYoutubeDL.behaviour = lambda opts, url: None

    download_id = client.post('/download', data={'url': VIDEO_URL}).json()['download_id']

    assert uuid.UUID(download_id).version == 7


@pytest.mark.parametrize(
    'download_id',
    [
        '0b7f6c1e-3f4a-4d2b-9c8e-1a2b3c4d5e6f',  # UUIDv4, the format of jobs created before UUIDv7
        '019a3b2c-4d5e-7f00-8a1b-2c3d4e5f6a7b',
        '00000000-0000-0000-0000-000000000000',  # unknown jobs get the page too, it shows "Download not found"
    ],
)
def test_job_page_is_served_for_any_uuid(client, download_id):
    response = client.get(f'/jobs/{download_id}')

    assert response.status_code == 200
    assert response.text == client.get('/').text


def test_job_page_rejects_malformed_id(client):
    assert client.get('/jobs/not-a-uuid').status_code == 422


def test_status_of_unknown_or_malformed_id(client):
    assert client.get('/status/00000000-0000-0000-0000-000000000000').status_code == 404
    assert client.get('/status/not-a-uuid').status_code == 422
    assert client.get('/downloads/not-a-uuid/file.mp4').status_code == 422


def test_saved_metadata_is_readable_by_others(tmp_path):
    storage.save_metadata(tmp_path, {'status': 'pending'})

    assert (tmp_path / 'metadata.json').stat().st_mode & 0o777 == 0o644


def test_save_metadata_is_atomic(tmp_path, monkeypatch):
    storage.save_metadata(tmp_path, {'status': 'pending'})

    def failing_dump(*args, **kwargs):
        raise OSError('disk full')

    monkeypatch.setattr(storage.json, 'dump', failing_dump)
    with pytest.raises(OSError):
        storage.save_metadata(tmp_path, {'status': 'completed'})

    assert json.loads((tmp_path / 'metadata.json').read_text()) == {'status': 'pending'}
    assert [p.name for p in tmp_path.iterdir()] == ['metadata.json']


YOUTUBE_COOKIES = '# Netscape HTTP Cookie File\n.youtube.com\tTRUE\t/\tTRUE\t0\tSID\tsecret\n'


def bot_checked():
    raise_like_ytdlp(
        ExtractorError(
            'Sign in to confirm you’re not a bot. Use --cookies-from-browser or --cookies for the authentication. '
            'See  https://github.com/yt-dlp/yt-dlp/wiki/FAQ#how-do-i-pass-cookies-to-yt-dlp  for how to manually pass '
            'cookies. Also see  https://github.com/yt-dlp/yt-dlp/wiki/Extractors#exporting-youtube-cookies  for tips on '
            'effectively exporting YouTube cookies',
            expected=True,
            ie='youtube',
            video_id='dQw4w9WgXcQ',
        )
    )


def test_bot_check_is_needs_auth():
    with pytest.raises(Exception) as exc_info:
        bot_checked()

    assert errors.classify_error(exc_info.value)['error_code'] == 'needs_auth'


def fail_without_cookies_then(retry):
    """yt-dlp behaviour that asks for a login unless a cookiefile is passed, then runs retry(opts)"""

    def behaviour(opts, url):
        if 'cookiefile' not in opts:
            write_media(opts, 'leftover.f137.mp4.part')
            bot_checked()
        return retry(opts)

    return behaviour


@pytest.fixture
def instagram_cookies(downloads_dir):
    path = downloads_dir.parent / 'instagram_cookies.txt'
    path.write_text('# Netscape HTTP Cookie File\n')
    return path


def test_youtube_without_cookies_file_fails_with_needs_auth(client, downloads_dir, instagram_cookies):
    FakeYoutubeDL.behaviour = fail_without_cookies_then(lambda opts: pytest.fail('must not retry'))

    download_id = client.post('/download', data={'url': VIDEO_URL}).json()['download_id']

    metadata = read_metadata(downloads_dir, download_id)
    assert metadata['status'] == 'failed'
    assert metadata['error_code'] == 'needs_auth'
    assert metadata['used_youtube_cookies'] is False
    (ydl,) = FakeYoutubeDL.instances
    assert 'cookiefile' not in ydl.opts


def test_youtube_cookies_directory_is_treated_as_absent(client, downloads_dir):
    (downloads_dir.parent / 'youtube_cookies.txt').mkdir()
    FakeYoutubeDL.behaviour = fail_without_cookies_then(lambda opts: pytest.fail('must not retry'))

    download_id = client.post('/download', data={'url': VIDEO_URL}).json()['download_id']

    assert read_metadata(downloads_dir, download_id)['error_code'] == 'needs_auth'
    assert len(FakeYoutubeDL.instances) == 1


def test_youtube_needs_auth_is_retried_with_youtube_cookies(client, downloads_dir, instagram_cookies, caplog):
    cookies_file = downloads_dir.parent / 'youtube_cookies.txt'
    cookies_file.write_text(YOUTUBE_COOKIES)

    def retry(opts):
        assert opts['cookiefile'].read() == YOUTUBE_COOKIES
        return {'id': 'dQw4w9WgXcQ', 'requested_downloads': [{'filepath': write_media(opts, 'Video-dQw4w9WgXcQ.mp4')}]}

    FakeYoutubeDL.behaviour = fail_without_cookies_then(retry)

    download_id = client.post('/download', data={'url': VIDEO_URL}).json()['download_id']

    metadata = read_metadata(downloads_dir, download_id)
    assert metadata['status'] == 'completed'
    assert metadata['files'] == ['Video-dQw4w9WgXcQ.mp4']
    assert metadata['used_youtube_cookies'] is True
    first, second = FakeYoutubeDL.instances
    assert 'cookiefile' not in first.opts
    assert isinstance(second.opts['cookiefile'], io.StringIO)
    assert cookies_file.read_text() == YOUTUBE_COOKIES
    assert any(
        download_id in r.getMessage() and 'retrying with YouTube cookies' in r.getMessage() for r in caplog.records
    )
    assert 'retrying with YouTube cookies' in (downloads_dir / download_id / 'download.log').read_text()
    assert client.get(f'/status/{download_id}').json()['used_youtube_cookies'] is True


def test_youtube_cookies_file_path_comes_from_env(client, downloads_dir, monkeypatch):
    cookies_file = downloads_dir.parent / 'secrets' / 'yt.txt'
    cookies_file.parent.mkdir()
    cookies_file.write_text(YOUTUBE_COOKIES)
    monkeypatch.setenv('YOUTUBE_COOKIES_FILE', str(cookies_file))
    FakeYoutubeDL.behaviour = fail_without_cookies_then(
        lambda opts: {'id': 'x', 'requested_downloads': [{'filepath': write_media(opts, 'video-x.mp4')}]}
    )

    download_id = client.post('/download', data={'url': VIDEO_URL}).json()['download_id']

    assert read_metadata(downloads_dir, download_id)['status'] == 'completed'
    assert len(FakeYoutubeDL.instances) == 2


def test_failed_youtube_cookies_retry_reports_the_retry_error(client, downloads_dir):
    (downloads_dir.parent / 'youtube_cookies.txt').write_text(YOUTUBE_COOKIES)
    FakeYoutubeDL.behaviour = fail_without_cookies_then(lambda opts: rate_limited())

    download_id = client.post('/download', data={'url': VIDEO_URL}).json()['download_id']

    metadata = read_metadata(downloads_dir, download_id)
    assert metadata['status'] == 'failed'
    assert metadata['error_code'] == 'rate_limited'
    assert metadata['files'] == []
    assert metadata['used_youtube_cookies'] is True
    assert len(FakeYoutubeDL.instances) == 2


def test_youtube_rate_limit_is_not_retried_with_cookies(client, downloads_dir):
    (downloads_dir.parent / 'youtube_cookies.txt').write_text(YOUTUBE_COOKIES)

    def behaviour(opts, url):
        rate_limited()

    FakeYoutubeDL.behaviour = behaviour

    download_id = client.post('/download', data={'url': VIDEO_URL}).json()['download_id']

    metadata = read_metadata(downloads_dir, download_id)
    assert metadata['error_code'] == 'rate_limited'
    assert metadata['used_youtube_cookies'] is False
    assert len(FakeYoutubeDL.instances) == 1


def test_instagram_uses_instagram_cookies_without_retry(client, downloads_dir, instagram_cookies):
    (downloads_dir.parent / 'youtube_cookies.txt').write_text(YOUTUBE_COOKIES)

    def behaviour(opts, url):
        instagram_restricted()

    FakeYoutubeDL.behaviour = behaviour

    download_id = client.post('/download', data={'url': 'https://www.instagram.com/p/ABC123/'}).json()['download_id']

    metadata = read_metadata(downloads_dir, download_id)
    assert metadata['error_code'] == 'needs_auth'
    assert 'used_youtube_cookies' not in metadata
    (ydl,) = FakeYoutubeDL.instances
    assert ydl.opts['cookiefile'] == 'instagram_cookies.txt'


def test_other_sites_get_no_cookies(client, downloads_dir, instagram_cookies):
    FakeYoutubeDL.behaviour = lambda opts, url: {'requested_downloads': [{'filepath': write_media(opts, 'clip.mp4')}]}

    download_id = client.post('/download', data={'url': 'https://vimeo.com/123456789'}).json()['download_id']

    assert read_metadata(downloads_dir, download_id)['status'] == 'completed'
    (ydl,) = FakeYoutubeDL.instances
    assert 'cookiefile' not in ydl.opts


LEGACY_JOB_ID = '0b7f6c1e-3f4a-4d2b-9c8e-1a2b3c4d5e6f'


def write_legacy_job(downloads_dir, **fields) -> Path:
    """A job as the service stored it before artifacts, subtitles and completed_at existed"""
    job_dir = downloads_dir / LEGACY_JOB_ID
    job_dir.mkdir(parents=True)
    (job_dir / 'Old_Video-abc.mp4').write_bytes(b'old video')
    (job_dir / 'download.log').write_text('log')
    metadata = {
        'download_id': LEGACY_JOB_ID,
        'url': VIDEO_URL,
        'status': 'completed',
        'files': ['Old_Video-abc.mp4', 'download.log'],
        'error': None,
        'created_at': '2025-03-01T10:00:00',
        'updated_at': '2025-03-01T10:05:00',
        'title': 'Old Video',
    } | fields
    (job_dir / 'metadata.json').write_text(json.dumps(metadata))
    return job_dir


def test_legacy_job_does_not_list_or_serve_its_log(client, downloads_dir):
    write_legacy_job(downloads_dir)

    status = client.get(f'/status/{LEGACY_JOB_ID}').json()

    assert status['status'] == 'completed'
    assert status['files'] == ['Old_Video-abc.mp4']
    assert client.get(f'/downloads/{LEGACY_JOB_ID}/Old_Video-abc.mp4').content == b'old video'
    assert client.get(f'/downloads/{LEGACY_JOB_ID}/download.log').status_code == 404


def youtube_video(opts, url):
    """A YouTube video in Czech with automatic captions only, like most videos without uploaded subtitles"""
    return {
        'id': 'abc',
        'title': 'Czech Video',
        'language': None,
        'subtitles': {'live_chat': [{'ext': 'json'}]},
        'automatic_captions': {key: [{'ext': 'vtt'}] for key in ('cs-orig', 'cs', 'en', 'de', 'fr')},
        'requested_downloads': [{'filepath': write_media(opts, 'Czech_Video-abc.mp4')}],
    }


def write_requested_subtitles(params, info):
    for pattern in params['subtitleslangs']:
        key = pattern.replace('\\', '')
        write_media(params, f'Czech_Video-abc.{key}.vtt', f'WEBVTT {key}'.encode())


def test_download_extracts_once_and_fetches_subtitles_and_details(client, downloads_dir):
    FakeYoutubeDL.behaviour = youtube_video
    FakeYoutubeDL.subtitles_behaviour = write_requested_subtitles

    download_id = client.post('/download', data={'url': VIDEO_URL}).json()['download_id']

    (ydl,) = FakeYoutubeDL.instances
    assert ydl.calls == [(VIDEO_URL, False)]
    media_pass, subtitles_pass = ydl.processed
    assert media_pass['writesubtitles'] is False
    assert media_pass['writeautomaticsub'] is False
    assert media_pass['updatetime'] is False
    assert not media_pass.get('skip_download')
    assert subtitles_pass['skip_download'] is True
    assert subtitles_pass['subtitleslangs'] == ['cs\\-orig', 'en']
    assert subtitles_pass['subtitlesformat'] == 'vtt/best'

    metadata = read_metadata(downloads_dir, download_id)
    assert metadata['status'] == 'completed'
    assert metadata['files'] == ['Czech_Video-abc.mp4']
    assert metadata['subtitles_error'] is None
    assert metadata['completed_at'] >= metadata['created_at']
    assert metadata['artifacts'] == [
        {'name': 'Czech_Video-abc.mp4', 'kind': 'media', 'size': 5},
        {'name': 'Czech_Video-abc.cs-orig.vtt', 'kind': 'subtitle', 'size': 14, 'lang': 'cs-orig', 'auto': True},
        {'name': 'Czech_Video-abc.en.vtt', 'kind': 'subtitle', 'size': 9, 'lang': 'en', 'auto': True},
        {'name': 'info.json', 'kind': 'info', 'size': (downloads_dir / download_id / 'info.json').stat().st_size},
    ]
    info = json.loads((downloads_dir / download_id / 'info.json').read_text())
    assert info['title'] == 'Czech Video'

    status = client.get(f'/status/{download_id}').json()
    assert [a['name'] for a in status['artifacts']] == [a['name'] for a in metadata['artifacts']]
    assert status['completed_at'] == metadata['completed_at']
    assert client.get(f'/downloads/{download_id}/Czech_Video-abc.en.vtt').content == b'WEBVTT en'
    assert client.get(f'/downloads/{download_id}/info.json').json()['id'] == 'abc'
    assert client.get(f'/downloads/{download_id}/metadata.json').status_code == 404
    assert client.get(f'/downloads/{download_id}/download.log').status_code == 404


def test_failed_subtitles_keep_the_job_completed(client, downloads_dir, caplog):
    def failing_subtitles(params, info):
        write_media(params, 'Czech_Video-abc.cs-orig.vtt', b'WEBVTT')
        write_media(params, 'Czech_Video-abc.en.vtt.part', b'WEB')
        raise_like_ytdlp(ExtractorError("Unable to download video subtitles for 'en': HTTP Error 429"))

    FakeYoutubeDL.behaviour = youtube_video
    FakeYoutubeDL.subtitles_behaviour = failing_subtitles

    download_id = client.post('/download', data={'url': VIDEO_URL}).json()['download_id']

    metadata = read_metadata(downloads_dir, download_id)
    assert metadata['status'] == 'completed'
    assert metadata['error'] is None
    assert metadata['files'] == ['Czech_Video-abc.mp4']
    assert 'HTTP Error 429' in metadata['subtitles_error']
    assert [(a['name'], a['kind']) for a in metadata['artifacts']] == [
        ('Czech_Video-abc.mp4', 'media'),
        ('Czech_Video-abc.cs-orig.vtt', 'subtitle'),
        ('info.json', 'info'),
    ]
    assert any(download_id in r.getMessage() and 'subtitles failed' in r.getMessage() for r in caplog.records)
    assert 'Subtitles failed' in (downloads_dir / download_id / 'download.log').read_text()


def test_video_without_subtitles_skips_the_subtitles_pass(client, downloads_dir):
    FakeYoutubeDL.behaviour = lambda opts, url: {
        'id': 'x',
        'requested_downloads': [{'filepath': write_media(opts, 'video-x.mp4')}],
    }
    FakeYoutubeDL.subtitles_behaviour = lambda params, info: pytest.fail('no subtitles to fetch')

    download_id = client.post('/download', data={'url': 'https://www.instagram.com/p/ABC123/'}).json()['download_id']

    (ydl,) = FakeYoutubeDL.instances
    assert len(ydl.processed) == 1
    metadata = read_metadata(downloads_dir, download_id)
    assert [a['kind'] for a in metadata['artifacts']] == ['media', 'info']


def test_youtube_cookies_retry_fetches_subtitles_with_the_cookies(client, downloads_dir):
    (downloads_dir.parent / 'youtube_cookies.txt').write_text(YOUTUBE_COOKIES)
    FakeYoutubeDL.behaviour = fail_without_cookies_then(lambda opts: youtube_video(opts, VIDEO_URL))
    FakeYoutubeDL.subtitles_behaviour = write_requested_subtitles

    download_id = client.post('/download', data={'url': VIDEO_URL}).json()['download_id']

    metadata = read_metadata(downloads_dir, download_id)
    assert metadata['status'] == 'completed'
    assert metadata['used_youtube_cookies'] is True
    assert [a['kind'] for a in metadata['artifacts']] == ['media', 'subtitle', 'subtitle', 'info']
    first, second = FakeYoutubeDL.instances
    assert first.processed == []
    assert [p.get('skip_download') for p in second.processed] == [None, True]


def test_legacy_job_lists_its_media_as_artifacts(client, downloads_dir):
    write_legacy_job(downloads_dir)

    status = client.get(f'/status/{LEGACY_JOB_ID}').json()

    assert status['artifacts'] == [
        {'name': 'Old_Video-abc.mp4', 'kind': 'media', 'size': 9, 'lang': None, 'auto': None}
    ]
    assert status['completed_at'] is None


def test_page_sets_the_csrf_cookie_and_embeds_the_same_token(downloads_dir):
    with TestClient(web.app) as fresh:
        response = fresh.get('/')
        again = fresh.get(f'/jobs/{LEGACY_JOB_ID}')

    token = response.cookies['csrf_token']
    assert f'content="{token}"' in response.text
    assert f'content="{token}"' in again.text
    cookie = response.headers['set-cookie'].lower()
    assert 'httponly' in cookie
    assert 'samesite=strict' in cookie
    assert 'secure' not in cookie
    assert response.headers['cache-control'] == 'no-store'


def test_csrf_cookie_is_secure_behind_the_https_proxy(downloads_dir):
    with TestClient(web.app) as fresh:
        response = fresh.get('/', headers={'X-Forwarded-Proto': 'https'})

    assert '; secure' in response.headers['set-cookie'].lower()


@pytest.mark.parametrize(
    'headers',
    [
        {'X-CSRF-Token': None},
        {'X-CSRF-Token': 'A' * 43},
        {'Sec-Fetch-Site': 'cross-site'},
        {'Sec-Fetch-Site': 'same-site'},
        {'Origin': 'https://evil.example'},
        {'Origin': 'null'},
    ],
)
def test_download_without_valid_csrf_is_forbidden(client, downloads_dir, headers):
    for name, value in headers.items():
        if value is None:
            client.headers.pop(name)
        else:
            client.headers[name] = value

    response = client.post('/download', data={'url': VIDEO_URL})

    assert response.status_code == 403
    assert isinstance(response.json()['detail'], str)
    assert list(downloads_dir.iterdir()) == []


def test_download_without_csrf_cookie_is_forbidden(client, downloads_dir):
    client.cookies.clear()

    assert client.post('/download', data={'url': VIDEO_URL}).status_code == 403


@pytest.mark.parametrize(
    'headers',
    [
        {'Sec-Fetch-Site': 'same-origin', 'Origin': 'http://testserver'},
        {'Sec-Fetch-Site': 'none'},
        {'Origin': 'https://video-download.example.ts.net', 'X-Forwarded-Host': 'video-download.example.ts.net'},
    ],
)
def test_download_from_the_page_passes_csrf(client, headers):
    FakeYoutubeDL.behaviour = lambda opts, url: None

    response = client.post('/download', data={'url': VIDEO_URL}, headers=headers)

    assert response.status_code == 200


def completed_job_with_subtitles(client) -> str:
    FakeYoutubeDL.behaviour = youtube_video
    FakeYoutubeDL.subtitles_behaviour = write_requested_subtitles
    return client.post('/download', data={'url': VIDEO_URL}).json()['download_id']


@pytest.mark.parametrize(
    ('filename', 'kind'),
    [('Czech_Video-abc.mp4', 'media'), ('Czech_Video-abc.en.vtt', 'subtitle'), ('info.json', 'info')],
)
def test_delete_removes_the_file_and_records_it(client, downloads_dir, filename, kind):
    download_id = completed_job_with_subtitles(client)
    size = (downloads_dir / download_id / filename).stat().st_size

    response = client.delete(f'/downloads/{download_id}/{filename}')

    assert response.status_code == 200
    assert not (downloads_dir / download_id / filename).exists()
    metadata = read_metadata(downloads_dir, download_id)
    assert filename not in metadata['files']
    assert filename not in [a['name'] for a in metadata['artifacts']]
    (deleted,) = metadata['deleted_files']
    assert deleted == {
        'name': filename,
        'kind': kind,
        'size': size,
        'deleted_at': deleted['deleted_at'],
        'reason': 'user',
    }
    assert response.json()['deleted_files'] == [deleted]
    assert filename not in [a['name'] for a in response.json()['artifacts']]
    assert client.get(f'/downloads/{download_id}/{filename}').status_code == 404
    assert client.delete(f'/downloads/{download_id}/{filename}').status_code == 404


def test_delete_of_legacy_job_media(client, downloads_dir):
    job_dir = write_legacy_job(downloads_dir)

    response = client.delete(f'/downloads/{LEGACY_JOB_ID}/Old_Video-abc.mp4')

    assert response.status_code == 200
    assert not (job_dir / 'Old_Video-abc.mp4').exists()
    assert (job_dir / 'download.log').exists()
    metadata = read_metadata(downloads_dir, LEGACY_JOB_ID)
    assert metadata['files'] == []
    assert metadata['artifacts'] == []
    assert metadata['deleted_files'][0]['size'] == 9


@pytest.mark.parametrize('filename', ['metadata.json', 'download.log', 'unknown.mp4', '..', '%2e%2e%2fsecret'])
def test_delete_refuses_private_unknown_and_outside_files(client, downloads_dir, filename):
    download_id = completed_job_with_subtitles(client)
    (downloads_dir / download_id / 'unknown.mp4').write_bytes(b'x')
    (downloads_dir / 'secret').write_text('secret')
    before = sorted(p.name for p in (downloads_dir / download_id).iterdir())

    response = client.delete(f'/downloads/{download_id}/{filename}')

    assert response.status_code in (404, 405)
    assert sorted(p.name for p in (downloads_dir / download_id).iterdir()) == before
    assert (downloads_dir / 'secret').exists()


@pytest.mark.parametrize('status', ['pending', 'downloading'])
def test_delete_refuses_while_the_job_runs(client, downloads_dir, status):
    job_dir = write_legacy_job(downloads_dir, status=status)

    response = client.delete(f'/downloads/{LEGACY_JOB_ID}/Old_Video-abc.mp4')

    assert response.status_code == 409
    assert (job_dir / 'Old_Video-abc.mp4').exists()


def test_delete_needs_the_csrf_token(client, downloads_dir):
    job_dir = write_legacy_job(downloads_dir)
    client.headers.pop('X-CSRF-Token')

    assert client.delete(f'/downloads/{LEGACY_JOB_ID}/Old_Video-abc.mp4').status_code == 403
    assert (job_dir / 'Old_Video-abc.mp4').exists()


def test_delete_of_unknown_job(client):
    assert client.delete('/downloads/00000000-0000-0000-0000-000000000000/x.mp4').status_code == 404
