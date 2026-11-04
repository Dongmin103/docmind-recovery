"""Keep long ingestion calls out of the event loop's control-request executor."""

import asyncio
import contextvars
import os
import threading
from concurrent.futures import ThreadPoolExecutor


class IngestionBusy(Exception):
    pass


class _IngestionExecutor:
    def __init__(self, capacity: int):
        if type(capacity) is not int or not 1 <= capacity <= 12:
            raise ValueError("Ingestion executor capacity must be between 1 and 12")
        self._slots = threading.BoundedSemaphore(capacity)
        self._threads = ThreadPoolExecutor(max_workers=capacity, thread_name_prefix="docmind-ingest")

    async def run(self, function, /, *args, **kwargs):
        if not self._slots.acquire(blocking=False):
            raise IngestionBusy("DOCMIND_INGESTION_BUSY")
        try:
            context = contextvars.copy_context()
            future = self._threads.submit(context.run, function, *args, **kwargs)
        except BaseException:
            self._slots.release()
            raise
        # Cancellation of the HTTP waiter must not free an executing job's slot.
        future.add_done_callback(lambda _: self._slots.release())
        wrapped = asyncio.wrap_future(future)
        # A disconnected caller may never retrieve the eventual worker exception.
        wrapped.add_done_callback(lambda done: None if done.cancelled() else done.exception())
        return await asyncio.shield(wrapped)


_executor = None
_executor_lock = threading.Lock()


async def run(function, /, *args, **kwargs):
    global _executor
    # Initialize after worker-process fork; settings change only after draining/restart.
    with _executor_lock:
        if _executor is None:
            _executor = _IngestionExecutor(int(os.environ.get("DOCMIND_INGESTION_DOCUMENT_CONCURRENCY", "1")))
    return await _executor.run(function, *args, **kwargs)
