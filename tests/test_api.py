import httpx
import pytest

from royalbooks.app import _run, app
from royalbooks.providers import Book, Provider
from royalbooks.storage import MetricsStore


class BrokenProvider(Provider):
    name = "broken"
    min_interval = 0

    async def search(self, client, query, language, limit):
        raise RuntimeError("secret token should not leak")


class GoodProvider(Provider):
    name = "good"
    min_interval = 0

    async def search(self, client, query, language, limit):
        return [Book(source="good", source_id="1", title="A", authors=[], language="de", year=None,
                     cover_url=None, info_url="https://example.org")]


@pytest.mark.asyncio
async def test_failed_provider_does_not_crash_others(tmp_path):
    app.state.store = MetricsStore(str(tmp_path / "test.sqlite3"))
    async with httpx.AsyncClient() as client:
        results = await __import__("asyncio").gather(
            _run(BrokenProvider(), client, "test", "de", 5),
            _run(GoodProvider(), client, "test", "de", 5),
        )
    assert results[0][1]["status"] == "error"
    assert results[0][1]["error"] == "RuntimeError"
    assert results[1][1]["status"] == "found"
    stats = app.state.store.stats()
    assert len(stats) == 2
    assert next(x for x in stats if x["provider"] == "good")["found"] == 1


@pytest.mark.asyncio
async def test_health_and_invalid_search(tmp_path):
    app.state.store = MetricsStore(str(tmp_path / "test.sqlite3"))
    app.state.providers = {"good": GoodProvider()}
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        assert (await client.get("/api/health")).json()["status"] == "ok"
        assert (await client.get("/api/search?q=a")).status_code == 422
        assert (await client.get("/api/search?q=test&language=xx")).status_code == 422
        response = await client.get("/api/search?q=test")
    assert response.status_code == 200
    assert response.json()["items"][0]["title"] == "A"
