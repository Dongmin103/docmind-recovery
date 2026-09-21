import asyncio
import hashlib
import logging
import os
import time
from collections import Counter
from typing import Any

from api.db.services.docmind_catalog_service import build_shadow_signature, compare_shadow_signature


logger = logging.getLogger(__name__)

_SAMPLE_RATE = 0.10
_ENQUEUE_DEADLINE_SECONDS = 0.010
_QUEUE_LIMIT = 128
_ENABLED_ENV = "DOCMIND_CATALOG_SHADOW_ENABLED"


class CatalogShadowSampler:
    def __init__(self, *, queue_limit: int = _QUEUE_LIMIT):
        self.queue_limit = queue_limit
        self._loop: asyncio.AbstractEventLoop | None = None
        self._queue: asyncio.Queue | None = None
        self._worker: asyncio.Task | None = None
        self.stats: Counter[str] = Counter()

    @staticmethod
    def should_sample(trace_id: str) -> bool:
        bucket = int(hashlib.sha256(trace_id.encode("utf-8")).hexdigest()[:8], 16) % 10
        return bucket == 0

    def enqueue(self, catalog: Any, tenant_id: str, trace_id: str) -> str:
        if os.environ.get(_ENABLED_ENV) != "1":
            self.stats["disabled"] += 1
            return "disabled"
        if not self.should_sample(trace_id):
            self.stats["not_sampled"] += 1
            return "not_sampled"

        loop = asyncio.get_running_loop()
        if self._loop is not loop:
            self._loop = loop
            self._queue = asyncio.Queue(maxsize=self.queue_limit)
            self._worker = None

        started = time.perf_counter()
        signature = build_shadow_signature(catalog)
        if time.perf_counter() - started > _ENQUEUE_DEADLINE_SECONDS:
            self.stats["enqueue_deadline_exceeded"] += 1
            return "enqueue_deadline_exceeded"
        try:
            self._queue.put_nowait((signature, tenant_id))
        except asyncio.QueueFull:
            self.stats["queue_full"] += 1
            return "queue_full"
        self.stats["enqueued"] += 1
        if self._worker is None or self._worker.done():
            self._worker = loop.create_task(self._drain())
        return "enqueued"

    async def _drain(self) -> None:
        while self._queue is not None:
            try:
                signature, tenant_id = self._queue.get_nowait()
            except asyncio.QueueEmpty:
                return
            try:
                comparison = await asyncio.to_thread(compare_shadow_signature, tenant_id, signature)
                metric = "match" if comparison.matched else f"mismatch_{comparison.reason.lower()}"
                self.stats[metric] += 1
                logger.info("DocMind catalog shadow outcome=%s", comparison.reason)
            except Exception:
                self.stats["worker_error"] += 1
                logger.warning("DocMind catalog shadow outcome=WORKER_ERROR")
            finally:
                self._queue.task_done()

    async def wait_idle(self) -> None:
        worker = self._worker
        if worker is not None:
            await worker


catalog_shadow_sampler = CatalogShadowSampler()


def maybe_enqueue_catalog_shadow(catalog: Any, tenant_id: str, trace_id: str) -> str:
    try:
        return catalog_shadow_sampler.enqueue(catalog, tenant_id, trace_id)
    except Exception:
        catalog_shadow_sampler.stats["enqueue_error"] += 1
        logger.warning("DocMind catalog shadow outcome=ENQUEUE_ERROR")
        return "enqueue_error"
