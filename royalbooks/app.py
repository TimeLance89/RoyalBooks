"""RoyalBooks discovery API. Search only; no acquisition or CWA mutation yet."""
from __future__ import annotations

import asyncio
import os
from contextlib import asynccontextmanager
from pathlib import Path
from time import perf_counter

import httpx
from fastapi import FastAPI, Query
from fastapi.responses import FileResponse

from royalbooks.providers import Book, Gutendex, OpenLibrary, Provider
from royalbooks.storage import MetricsStore


@asynccontextmanager
async def lifespan(app: FastAPI):
    app.state.store = MetricsStore(os.getenv("ROYALBOOKS_DB_PATH", "/data/royalbooks.sqlite3"))
    app.state.providers = {p.name: p for p in [Gutendex(), OpenLibrary()]}
    yield


app = FastAPI(title="RoyalBooks", version="0.1.0", lifespan=lifespan)


@app.get("/")
async def homepage():
    return FileResponse(Path(__file__).parent / "static" / "index.html")


@app.get("/api/health")
async def health():
    return {"status": "ok", "version": "0.1.0"}


@app.get("/api/providers")
async def providers():
    return {"providers": [{"name": p.name, "purpose": p.purpose} for p in app.state.providers.values()],
            "recent_metrics": app.state.store.stats()}


async def _run(provider: Provider, client: httpx.AsyncClient, q: str, language: str | None, limit: int):
    start = perf_counter()
    try:
        items = await asyncio.wait_for(provider.search(client, q, language, limit), timeout=18.0)
        outcome = "found" if items else "empty"
        error = None
    except Exception as exc:
        items = []
        outcome = "error"
        error = type(exc).__name__ if type(exc).__name__ != "ProviderError" else str(exc)[:50]
    duration_ms = int((perf_counter() - start) * 1000)
    await asyncio.to_thread(app.state.store.record, provider.name, outcome, len(items), duration_ms, error)
    return items, {"provider": provider.name, "status": outcome, "results": len(items),
                   "duration_ms": duration_ms, "error": error}


@app.get("/api/search")
async def search(
    q: str = Query(min_length=2, max_length=120),
    language: str = Query(default="de", pattern="^(de|en|all)$"),
    limit: int = Query(default=10, ge=1, le=30),
):
    requested_language = None if language == "all" else language
    agents = list(app.state.providers.values())
    async with httpx.AsyncClient(timeout=httpx.Timeout(7.0), follow_redirects=False,
                                 headers={"User-Agent": "RoyalBooks/0.1 (https://github.com/TimeLance89/RoyalBooks)"}) as client:
        results = await asyncio.gather(*[
            _run(provider, client, q, requested_language, limit) for provider in agents
        ])
    items: list[Book] = [item for books, _ in results for item in books]
    return {"query": q, "language": language, "items": [item.as_dict() for item in items],
            "sources": [status for _, status in results], "rights_notice":
            "US public-domain status is not proof of permission in your country; verify rights before download."}
