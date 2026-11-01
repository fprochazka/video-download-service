import json
import logging
import os
import threading
from dataclasses import replace
from datetime import UTC, datetime, timedelta

import pytest
from fastapi.testclient import TestClient

from video_download import cleanup, config, web

NOW = datetime(2026, 10, 31, 12, 0, tzinfo=UTC)
MB = 1024 * 1024
SETTINGS = config.GcSettings(
    enabled=True, dry_run=False, max_age=timedelta(days=90), min_size_bytes=5 * MB, interval_seconds=6 * 3600
)


def iso(days_ago: float) -> str:
    return (NOW - timedelta(days=days_ago)).isoformat()


def make_job(downloads_dir, job_id, files: dict[str, int], status='completed', artifacts=True, **times):
    """A job with files of the given sizes; names ending in .vtt are subtitles, info.json the details"""
    job_dir = downloads_dir / job_id
    job_dir.mkdir(parents=True)
    entries = []
    for name, size in files.items():
        with open(job_dir / name, 'wb') as f:
            f.truncate(size)
        kind = 'subtitle' if name.endswith('.vtt') else 'info' if name == 'info.json' else 'media'
        if not cleanup.is_partial(name):
            entries.append({'name': name, 'kind': kind, 'size': size})
    (job_dir / 'download.log').write_text('log')
    metadata = {
        'download_id': job_id,
        'status': status,
        'url': 'https://example.com',
        'files': [e['name'] for e in entries if e['kind'] == 'media'],
    }
    if artifacts:
        metadata['artifacts'] = entries
    else:
        metadata['files'].append('download.log')
    (job_dir / 'metadata.json').write_text(json.dumps(metadata | times))
    # A fresh download can carry a years-old mtime from the server's Last-Modified, it must not matter
    for path in job_dir.iterdir():
        os.utime(path, (0, 0))
    return job_dir


def names(job_dir):
    return sorted(p.name for p in job_dir.iterdir())


def metadata_of(job_dir):
    return json.loads((job_dir / 'metadata.json').read_text())


def test_old_large_media_is_deleted_and_the_rest_kept(downloads_dir, caplog):
    caplog.set_level(logging.INFO)
    job = make_job(
        downloads_dir,
        'old',
        {'big.mp4': 6 * MB, 'small.jpg': 4 * MB, 'big.en.vtt': 6 * MB, 'info.json': 6 * MB},
        created_at=iso(100),
        completed_at=iso(91),
    )

    summary = cleanup.collect_garbage(downloads_dir, NOW, SETTINGS)

    assert (summary.files, summary.bytes) == (1, 6 * MB)
    assert names(job) == ['big.en.vtt', 'download.log', 'info.json', 'metadata.json', 'small.jpg']
    metadata = metadata_of(job)
    assert metadata['files'] == ['small.jpg']
    assert [a['name'] for a in metadata['artifacts']] == ['small.jpg', 'big.en.vtt', 'info.json']
    (deleted,) = metadata['deleted_files']
    assert deleted | {'deleted_at': None} == {
        'name': 'big.mp4',
        'kind': 'media',
        'size': 6 * MB,
        'deleted_at': None,
        'reason': 'gc',
    }
    messages = [r.getMessage() for r in caplog.records]
    assert any('old/big.mp4' in m and 'deleted' in m for m in messages)
    assert any(m.startswith('GC finished') and '1 files' in m and '6.0 MB' in m for m in messages)


def test_recent_large_media_is_kept(downloads_dir):
    job = make_job(downloads_dir, 'new', {'big.mp4': 50 * MB}, created_at=iso(200), completed_at=iso(89))

    assert cleanup.collect_garbage(downloads_dir, NOW, SETTINGS).files == 0
    assert 'big.mp4' in names(job)


@pytest.mark.parametrize(
    ('times', 'deleted'),
    [
        ({'created_at': '2025-03-01T10:00:00', 'updated_at': '2025-03-01T10:05:00'}, True),
        ({'created_at': iso(200), 'updated_at': iso(10)}, False),
        ({'created_at': iso(91)}, True),
        ({}, False),
    ],
)
def test_legacy_job_age_comes_from_updated_at_then_created_at(downloads_dir, times, deleted):
    job = make_job(downloads_dir, 'legacy', {'big.mp4': 6 * MB}, artifacts=False, **times)

    cleanup.collect_garbage(downloads_dir, NOW, SETTINGS)

    assert ('big.mp4' not in names(job)) is deleted
    assert 'download.log' in names(job)
    if deleted:
        metadata = metadata_of(job)
        assert metadata['files'] == []
        assert metadata['artifacts'] == []
        assert metadata['deleted_files'][0]['reason'] == 'gc'


@pytest.mark.parametrize('status', ['pending', 'downloading'])
def test_running_job_is_skipped(downloads_dir, status):
    job = make_job(
        downloads_dir, 'running', {'big.mp4': 6 * MB, 'x.mp4.part': 6 * MB}, status=status, created_at=iso(400)
    )

    cleanup.collect_garbage(downloads_dir, NOW, SETTINGS)

    assert names(job) == ['big.mp4', 'download.log', 'metadata.json', 'x.mp4.part']


@pytest.mark.parametrize(('days_ago', 'deleted'), [(2, True), (0.5, False)])
def test_partials_of_failed_job_go_after_a_day_regardless_of_size(downloads_dir, days_ago, deleted):
    files = {'x.f137.mp4.part': 1000, 'x.f137.mp4.ytdl': 10, 'x.f137.mp4.part-Frag3': 10}
    job = make_job(downloads_dir, 'failed', files, status='failed', created_at=iso(days_ago), updated_at=iso(days_ago))

    cleanup.collect_garbage(downloads_dir, NOW, SETTINGS)

    remaining = set(names(job))
    assert remaining.isdisjoint(files) is deleted
    assert {'download.log', 'metadata.json'} <= remaining
    if deleted:
        assert {d['kind'] for d in metadata_of(job)['deleted_files']} == {'partial'}


def test_large_partials_of_old_completed_job_go(downloads_dir):
    job = make_job(downloads_dir, 'old', {'v.mp4': 1000, 'big.part': 6 * MB, 'small.part': 10}, completed_at=iso(100))

    cleanup.collect_garbage(downloads_dir, NOW, SETTINGS)

    assert names(job) == ['download.log', 'metadata.json', 'small.part', 'v.mp4']


def test_dry_run_deletes_nothing(downloads_dir, caplog):
    caplog.set_level(logging.INFO)
    job = make_job(downloads_dir, 'old', {'big.mp4': 6 * MB}, completed_at=iso(100))
    before = (job / 'metadata.json').read_text()

    summary = cleanup.collect_garbage(downloads_dir, NOW, replace(SETTINGS, dry_run=True))

    assert summary.files == 1
    assert 'big.mp4' in names(job)
    assert (job / 'metadata.json').read_text() == before
    assert any('would delete old/big.mp4' in r.getMessage() for r in caplog.records)


def test_symlinks_are_never_followed(downloads_dir, tmp_path):
    outside = tmp_path / 'outside'
    outside.mkdir()
    (outside / 'precious.mp4').write_bytes(b'x' * (6 * MB))
    job = make_job(downloads_dir, 'old', {}, completed_at=iso(100))
    (job / 'link.mp4').symlink_to(outside / 'precious.mp4')
    (job / 'link.part').symlink_to(outside / 'precious.mp4')
    metadata = metadata_of(job) | {'artifacts': [{'name': 'link.mp4', 'kind': 'media', 'size': 6 * MB}]}
    (job / 'metadata.json').write_text(json.dumps(metadata))
    (downloads_dir / 'linked-job').symlink_to(outside)

    cleanup.collect_garbage(downloads_dir, NOW, SETTINGS)

    assert (outside / 'precious.mp4').stat().st_size == 6 * MB
    assert (job / 'link.mp4').is_symlink()
    assert (job / 'link.part').is_symlink()


def test_job_without_metadata_or_stray_files_are_ignored(downloads_dir):
    downloads_dir.mkdir()
    (downloads_dir / 'stray').write_text('x')
    (downloads_dir / 'empty-job').mkdir()

    assert cleanup.collect_garbage(downloads_dir, NOW, SETTINGS).files == 0


def test_gc_settings_from_env(monkeypatch):
    assert config.gc_settings() == SETTINGS
    monkeypatch.setenv('GC_MAX_AGE_DAYS', '30')
    monkeypatch.setenv('GC_MIN_SIZE_MB', '0.5')
    monkeypatch.setenv('GC_INTERVAL_HOURS', '1')
    monkeypatch.setenv('GC_ENABLED', 'false')
    monkeypatch.setenv('GC_DRY_RUN', 'true')

    assert config.gc_settings() == config.GcSettings(
        enabled=False, dry_run=True, max_age=timedelta(days=30), min_size_bytes=MB // 2, interval_seconds=3600
    )


def test_startup_runs_gc_in_the_background(downloads_dir, monkeypatch):
    runs = []
    ran = threading.Event()

    def fake_collect(*args):
        runs.append(args)
        ran.set()

    monkeypatch.setattr(cleanup, 'collect_garbage', fake_collect)

    with TestClient(web.app):
        assert ran.wait(5)

    downloads, now, settings = runs[0]
    assert downloads == downloads_dir
    assert settings == SETTINGS


def test_gc_disabled_does_not_run(downloads_dir, monkeypatch):
    monkeypatch.setenv('GC_ENABLED', 'false')
    monkeypatch.setattr(cleanup, 'collect_garbage', lambda *args: pytest.fail('GC must not run'))

    with TestClient(web.app):
        pass
