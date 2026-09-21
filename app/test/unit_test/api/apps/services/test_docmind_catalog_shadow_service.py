import asyncio
import time
from types import SimpleNamespace

from api.apps.services import docmind_catalog_shadow_service as shadow


def _sample_trace(target: bool) -> str:
    for index in range(10_000):
        trace_id = f"trace-{index}"
        if shadow.CatalogShadowSampler.should_sample(trace_id) is target:
            return trace_id
    raise AssertionError("sample bucket not found")


def test_sampling_is_deterministic_and_bounded():
    sampled = _sample_trace(True)
    skipped = _sample_trace(False)

    assert shadow.CatalogShadowSampler.should_sample(sampled) is True
    assert shadow.CatalogShadowSampler.should_sample(sampled) is True
    assert shadow.CatalogShadowSampler.should_sample(skipped) is False


def test_sampler_is_disabled_by_default(monkeypatch):
    monkeypatch.delenv("DOCMIND_CATALOG_SHADOW_ENABLED", raising=False)
    sampler = shadow.CatalogShadowSampler()

    assert asyncio.run(_enqueue_and_wait(sampler, "trace-disabled")) == "disabled"
    assert sampler.stats == {"disabled": 1}


async def _enqueue_and_wait(sampler, trace_id, catalog=None):
    outcome = sampler.enqueue(catalog or SimpleNamespace(), "tenant-1", trace_id)
    await sampler.wait_idle()
    return outcome


def test_sampled_comparison_runs_off_request_and_records_match(monkeypatch):
    monkeypatch.setenv("DOCMIND_CATALOG_SHADOW_ENABLED", "1")
    monkeypatch.setattr(shadow.CatalogShadowSampler, "should_sample", staticmethod(lambda _trace_id: True))
    monkeypatch.setattr(shadow, "build_shadow_signature", lambda catalog: SimpleNamespace(marker=catalog.marker))
    monkeypatch.setattr(
        shadow,
        "compare_shadow_signature",
        lambda tenant_id, signature: SimpleNamespace(
            matched=tenant_id == "tenant-1" and signature.marker == "static", reason="MATCH"
        ),
    )
    sampler = shadow.CatalogShadowSampler()

    outcome = asyncio.run(_enqueue_and_wait(sampler, "trace-sampled", SimpleNamespace(marker="static")))

    assert outcome == "enqueued"
    assert sampler.stats["enqueued"] == 1
    assert sampler.stats["match"] == 1


def test_full_queue_is_dropped_without_blocking(monkeypatch):
    monkeypatch.setenv("DOCMIND_CATALOG_SHADOW_ENABLED", "1")
    monkeypatch.setattr(shadow.CatalogShadowSampler, "should_sample", staticmethod(lambda _trace_id: True))
    monkeypatch.setattr(shadow, "build_shadow_signature", lambda _catalog: SimpleNamespace())
    monkeypatch.setattr(
        shadow,
        "compare_shadow_signature",
        lambda *_args: SimpleNamespace(matched=True, reason="MATCH"),
    )

    async def run():
        sampler = shadow.CatalogShadowSampler(queue_limit=1)
        first = sampler.enqueue(SimpleNamespace(), "tenant-1", "trace-1")
        second = sampler.enqueue(SimpleNamespace(), "tenant-1", "trace-2")
        await sampler.wait_idle()
        return sampler, first, second

    sampler, first, second = asyncio.run(run())

    assert first == "enqueued"
    assert second == "queue_full"
    assert sampler.stats["queue_full"] == 1


def test_slow_signature_build_is_abandoned_before_queueing(monkeypatch):
    monkeypatch.setenv("DOCMIND_CATALOG_SHADOW_ENABLED", "1")
    monkeypatch.setattr(shadow.CatalogShadowSampler, "should_sample", staticmethod(lambda _trace_id: True))

    def slow_signature(_catalog):
        time.sleep(0.02)
        return SimpleNamespace()

    monkeypatch.setattr(shadow, "build_shadow_signature", slow_signature)
    sampler = shadow.CatalogShadowSampler()

    assert asyncio.run(_enqueue_and_wait(sampler, "trace-slow")) == "enqueue_deadline_exceeded"
    assert sampler.stats["enqueue_deadline_exceeded"] == 1
    assert sampler.stats["enqueued"] == 0


def test_worker_error_never_escapes_to_served_request(monkeypatch):
    monkeypatch.setenv("DOCMIND_CATALOG_SHADOW_ENABLED", "1")
    monkeypatch.setattr(shadow.CatalogShadowSampler, "should_sample", staticmethod(lambda _trace_id: True))
    monkeypatch.setattr(shadow, "build_shadow_signature", lambda _catalog: SimpleNamespace())
    monkeypatch.setattr(
        shadow,
        "compare_shadow_signature",
        lambda *_args: (_ for _ in ()).throw(RuntimeError("db unavailable")),
    )
    sampler = shadow.CatalogShadowSampler()

    assert asyncio.run(_enqueue_and_wait(sampler, "trace-error")) == "enqueued"
    assert sampler.stats["worker_error"] == 1
