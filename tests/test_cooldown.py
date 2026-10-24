"""Access-refusal cooldown: stop repeated requests instead of bypassing blocks."""
import asyncio
from time import monotonic

import httpx
import pytest

from royalbooks.benchmark import probe
from royalbooks.jobs import JobStore, worker
from royalbooks.providers import Gutendex, ProviderError


@pytest.mark.asyncio
async def test_blocked_catalog_enforces_cooldown_without_second_http_call():
    requests = 0
    def handler(request):
        nonlocal requests
        requests += 1
        return httpx.Response(403, text="Access denied")
    source = Gutendex()
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(ProviderError, match="http_403"):
            await source.get_json(client, "https://gutendex.com/books", {"search": "test"})
        with pytest.raises(ProviderError, match="source_cooldown_http_403"):
            await source.get_json(client, "https://gutendex.com/books", {"search": "test"})
    assert source.cooldown_remaining() > 1700
    assert requests == 1


@pytest.mark.asyncio
async def test_benchmark_retains_origin_status():
    source = Gutendex()
    async with httpx.AsyncClient(transport=httpx.MockTransport(lambda _: httpx.Response(403))) as client:
        outcome = await probe(source, {"query": "Faust", "title": "Faust", "language": "de"}, client)
    assert outcome["error_code"] == "http_403"


@pytest.mark.asyncio
async def test_blocked_source_does_not_consume_queued_attempts(tmp_path):
    source = Gutendex()
    source._blocked_until = monotonic() + 20
    store = JobStore(str(tmp_path / "jobs.db"))
    item = store.add(12345)
    task = asyncio.create_task(worker(store, tmp_path, None, source))
    try:
        await asyncio.sleep(0.15)
    finally:
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    record = store.get(item["id"])
    assert record["status"] == "queued"
    assert record["attempts"] == 0
