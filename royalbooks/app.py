"""RoyalBooks provider search and authenticated acquisition queue."""
from __future__ import annotations

import asyncio
import os
from contextlib import asynccontextmanager, suppress
from pathlib import Path
from time import perf_counter

import httpx
from fastapi import FastAPI, Header, HTTPException, Query, Path as ApiPath
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field

from royalbooks.jobs import JobStateError, JobStore, require_token, worker
from royalbooks.handoff import observe_handoff
from royalbooks.providers import Book, GoogleBooks, Gutendex, OpenLibrary, Provider
from royalbooks.source_registry import configured_providers
from royalbooks.matching import match_groups
from royalbooks.storage import MetricsStore


@asynccontextmanager
async def lifespan(app: FastAPI):
    path = os.getenv('ROYALBOOKS_DB_PATH', '/data/royalbooks.sqlite3')
    app.state.store = MetricsStore(path)
    app.state.jobs = JobStore(path)
    sources = configured_providers()
    app.state.providers = {p.name: p for p in sources}
    data = Path(path).parent
    ingest_path = os.getenv('ROYALBOOKS_CWA_INGEST_PATH', '').strip()
    task = asyncio.create_task(worker(app.state.jobs, data, Path(ingest_path) if ingest_path else None,
                                      app.state.providers.get('gutendex')))
    try:
        yield
    finally:
        task.cancel()
        with suppress(asyncio.CancelledError):
            await task


app = FastAPI(title='RoyalBooks', version='0.3.2', lifespan=lifespan)


@app.get('/')
async def homepage():
    return FileResponse(Path(__file__).parent / 'static' / 'index.html')


@app.get('/api/health')
async def health():
    return {'status': 'ok', 'version': '0.3.2'}


@app.get('/api/providers')
async def providers():
    return {'providers': [{'name': p.name, 'purpose': p.purpose} for p in app.state.providers.values()],
            'recent_metrics': app.state.store.stats()}


async def _run(provider: Provider, client: httpx.AsyncClient, q: str, language: str | None, limit: int):
    start = perf_counter()
    try:
        items = await asyncio.wait_for(provider.search(client, q, language, limit), timeout=18.0)
        outcome = 'found' if items else 'empty'
        error = None
    except Exception as exc:
        items = []
        outcome = 'error'
        error = type(exc).__name__ if type(exc).__name__ != 'ProviderError' else str(exc)[:50]
    duration_ms = int((perf_counter() - start) * 1000)
    await asyncio.to_thread(app.state.store.record, provider.name, outcome, len(items), duration_ms, error)
    return items, {'provider': provider.name, 'status': outcome, 'results': len(items),
                   'duration_ms': duration_ms, 'error': error}


@app.get('/api/search')
async def search(q: str = Query(min_length=2, max_length=120),
                 language: str = Query(default='de', pattern='^(de|en|all)$'),
                 limit: int = Query(default=10, ge=1, le=30)):
    requested_language = None if language == 'all' else language
    agents = list(app.state.providers.values())
    async with httpx.AsyncClient(timeout=httpx.Timeout(7.0), follow_redirects=False,
                                 headers={'User-Agent': 'RoyalBooks/0.2 (https://github.com/TimeLance89/RoyalBooks)'}) as client:
        results = await asyncio.gather(*[_run(provider, client, q, requested_language, limit) for provider in agents])
    items: list[Book] = [item for books, _ in results for item in books]
    return {'query': q, 'language': language, 'items': [item.as_dict() for item in items],
            'match_groups': match_groups(items),
            'sources': [status for _, status in results], 'rights_notice':
            'Public-domain classification may be US-only; check German law before requesting a download.'}


class QueueRequest(BaseModel):
    provider: str = 'gutendex'
    source_id: int = Field(gt=0, le=99999999)
    rights_confirmed: bool = False


def _guard(token: str | None) -> None:
    try:
        require_token(os.getenv('ROYALBOOKS_API_TOKEN'), token)
    except PermissionError as exc:
        status = 503 if str(exc) == 'acquisition_disabled_without_token' else 401
        raise HTTPException(status_code=status, detail=str(exc)) from exc


@app.post('/api/jobs', status_code=202)
async def enqueue(request: QueueRequest, x_api_key: str | None = Header(default=None)):
    _guard(x_api_key)
    if request.provider != 'gutendex':
        raise HTTPException(status_code=400, detail='only_gutendex_downloads_supported')
    if not request.rights_confirmed:
        raise HTTPException(status_code=400, detail='local_rights_confirmation_required')
    return {'job': app.state.jobs.add(request.source_id)}


@app.get('/api/jobs')
async def list_jobs(x_api_key: str | None = Header(default=None)):
    _guard(x_api_key)
    ingest_path = os.getenv('ROYALBOOKS_CWA_INGEST_PATH', '').strip()
    ingest = Path(ingest_path) if ingest_path else None
    staging = Path(app.state.jobs.path).parent / 'staging'
    records = await asyncio.to_thread(app.state.jobs.list)
    for job in records:
        job['handoff_observation'] = await asyncio.to_thread(observe_handoff, job, staging, ingest)
    return {'jobs': records, 'ingest_configured': bool(ingest_path),
            'handoff_notice': 'A disappearing ingest file does not confirm a successful Calibre import.'}


class RetryRequest(BaseModel):
    rights_confirmed: bool = False


@app.post('/api/jobs/{job_id}/retry', status_code=202)
async def retry_job(
    request: RetryRequest,
    job_id: str = ApiPath(min_length=32, max_length=32, pattern=r'^[0-9a-f]{32}$'),
    x_api_key: str | None = Header(default=None),
):
    _guard(x_api_key)
    if not request.rights_confirmed:
        raise HTTPException(status_code=400, detail='local_rights_confirmation_required')
    try:
        updated = await asyncio.to_thread(app.state.jobs.retry_failed, job_id)
        return {'job': updated}
    except JobStateError as exc:
        code = 404 if str(exc) == 'job_not_found' else 409
        raise HTTPException(status_code=code, detail=str(exc)) from exc
