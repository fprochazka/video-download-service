import asyncio
import logging

import pytest

from video_download import config
from video_download.worker import JobWorker


def run(coro):
    return asyncio.run(asyncio.wait_for(coro, 5))


def test_lanes_run_at_most_their_concurrency_at_once():
    async def scenario():
        worker = JobWorker({'a': 2, 'b': 1})
        worker.start()
        running = {'a': 0, 'b': 0}
        peak = {'a': 0, 'b': 0}

        async def job(lane):
            running[lane] += 1
            peak[lane] = max(peak[lane], running[lane])
            await asyncio.sleep(0.01)
            running[lane] -= 1

        for _ in range(5):
            worker.enqueue('a', job, 'a')
            worker.enqueue('b', job, 'b')
        await worker.join()
        await worker.stop()
        return peak

    assert run(scenario()) == {'a': 2, 'b': 1}


def test_a_failing_job_does_not_stop_the_lane(caplog):
    async def scenario():
        worker = JobWorker({'a': 1})
        worker.start()
        done = []

        async def failing():
            raise RuntimeError('boom')

        async def ok():
            done.append(True)

        worker.enqueue('a', failing)
        worker.enqueue('a', ok)
        await worker.join()
        await worker.stop()
        return done

    assert run(scenario()) == [True]
    assert any('failing' in r.getMessage() and r.levelno == logging.ERROR for r in caplog.records)


def test_join_waits_for_jobs_enqueued_by_jobs():
    async def scenario():
        worker = JobWorker({'a': 1, 'b': 1})
        worker.start()
        done = []

        async def second():
            await asyncio.sleep(0.01)
            done.append('second')

        async def first():
            worker.enqueue('b', second)
            done.append('first')

        worker.enqueue('a', first)
        await worker.join()
        await worker.stop()
        return done

    assert run(scenario()) == ['first', 'second']


def test_stop_cancels_the_running_job():
    async def scenario():
        worker = JobWorker({'a': 1})
        worker.start()
        started = asyncio.Event()
        cancelled = []

        async def forever():
            started.set()
            try:
                await asyncio.sleep(3600)
            except asyncio.CancelledError:
                cancelled.append(True)
                raise

        worker.enqueue('a', forever)
        await started.wait()
        await worker.stop()
        return cancelled

    assert run(scenario()) == [True]


def test_enqueue_on_unknown_lane_raises():
    worker = JobWorker({'a': 1})

    with pytest.raises(RuntimeError):
        worker.enqueue('a', lambda: None)


def test_download_concurrency_from_env(monkeypatch):
    assert config.download_concurrency() == 2
    monkeypatch.setenv('DOWNLOAD_CONCURRENCY', '5')
    assert config.download_concurrency() == 5
