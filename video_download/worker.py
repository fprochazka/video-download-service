"""The in-process job worker: queues of jobs, each lane with its own concurrency limit.

Jobs are started by enqueueing a coroutine function and its arguments on a lane, from any code running on the app's event
loop, not only from an HTTP request. The worker lives as long as the app: the lifespan starts it and stops it. Jobs still
queued or running when it stops are lost; their metadata stays pending or running, and the startup sweep marks them
interrupted on the next start.
"""

import asyncio
import contextlib
import logging
from collections.abc import Awaitable, Callable

logger = logging.getLogger(__name__)

DOWNLOAD = 'download'

type Job = Callable[..., Awaitable[object]]


class JobWorker:
    def __init__(self, concurrency: dict[str, int]):
        """concurrency maps each lane to the number of its jobs that run at the same time"""
        self.concurrency = {lane: max(1, limit) for lane, limit in concurrency.items()}
        self.queues: dict[str, asyncio.Queue] = {}
        self.consumers: list[asyncio.Task] = []
        # Jobs queued or running on any lane, join() waits for it to reach zero
        self.unfinished = 0
        self.idle = asyncio.Event()
        self.idle.set()

    def start(self):
        for lane, limit in self.concurrency.items():
            queue = asyncio.Queue()
            self.queues[lane] = queue
            self.consumers += [asyncio.create_task(self.consume(lane, queue)) for _ in range(limit)]

    async def stop(self):
        for consumer in self.consumers:
            consumer.cancel()
        for consumer in self.consumers:
            with contextlib.suppress(asyncio.CancelledError):
                await consumer
        self.consumers = []
        left = sum(queue.qsize() for queue in self.queues.values())
        if left:
            logger.warning('Worker stopped with %d queued jobs, the next start marks them interrupted', left)

    def enqueue(self, lane: str, job: Job, *args):
        """Queue job(*args) on the lane; call it on the event loop the worker was started on"""
        if lane not in self.queues:
            raise RuntimeError(f'The worker is not running or has no lane {lane}')
        self.queues[lane].put_nowait((job, args))
        self.unfinished += 1
        self.idle.clear()

    async def join(self):
        """Wait until every queued job ended, including jobs the jobs enqueued meanwhile"""
        await self.idle.wait()

    async def consume(self, lane: str, queue: asyncio.Queue):
        while True:
            job, args = await queue.get()
            try:
                await job(*args)
            except Exception:
                # The jobs record their own failures, this only keeps the consumer alive
                logger.exception('Job %s on lane %s raised', getattr(job, '__name__', job), lane)
            finally:
                queue.task_done()
                self.unfinished -= 1
                if self.unfinished == 0:
                    self.idle.set()
