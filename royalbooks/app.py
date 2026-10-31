"""RoyalBooks provider search and authenticated acquisition queue."""
from __future__ import annotations

import asyncio
import ipaddress
import os
import re
from contextlib import asynccontextmanager, suppress
from pathlib import Path
from time import perf_counter

import httpx
from fastapi import FastAPI, Header, HTTPException, Query, Request, Depends, Path as ApiPath
from fastapi.responses import JSONResponse
from fastapi.responses import FileResponse
from fastapi.responses import Response
from pydantic import BaseModel, Field, field_validator

from royalbooks.direct_sources import SOURCES, lookup_epub as lookup_direct_epub
from royalbooks.jobs import JobStateError, JobStore, require_token, worker
from royalbooks.gutenberg_catalog import catalog_status
from royalbooks.gutenberg_formats import format_status, lookup_epub
from royalbooks.handoff import observe_handoff
from royalbooks.bookorbit import Config as BookOrbitConfig, Client as BookOrbitClient, Handoffs, BookOrbitError
from royalbooks.preflight import diagnose
from royalbooks.catalog_admin import CatalogImportService, CatalogImportBusy
from royalbooks.watchlist import WatchlistStore, WatchlistFull
from typing import Literal
from royalbooks.providers import Book, GoogleBooks, Gutendex, OpenLibrary, Provider, ProviderError
from royalbooks.source_registry import configured_providers
from royalbooks.indexer_search import search_indexers
from royalbooks.discovery import SearchCoordinator, analyze_query, bounded_response
from royalbooks.source_profiles import profiles_for, search_evidence
from royalbooks.matching import match_groups
from royalbooks.matching import normalize, normalize_person
from royalbooks.presentation import display_groups
from royalbooks.storage import MetricsStore
from royalbooks.auth import SessionStore, COOKIE, SESSION_SECONDS
from royalbooks.library import bookshelf, library_file, LibraryUnavailable
from royalbooks.storefront import Storefront
from royalbooks.genres import Genres,GENRES,manifest as genre_manifest
from royalbooks.artwork import fetch_artwork
from royalbooks.settings import PlatformSettings
from royalbooks.availability import Availability
from royalbooks.providers import canonical_isbn
from royalbooks.catalog_labels import remember, apply_labels
from royalbooks.hoster_jobs import HosterJobs, worker as hoster_worker
from royalbooks.engine_status import read_status as engine_status


def downloads_enabled() -> bool:
    return (os.getenv('ROYALBOOKS_DOWNLOADS_ENABLED', '1') != '0'
            and os.getenv('ROYALBOOKS_LIBRARY_BACKEND', 'bookorbit') == 'bookorbit')


@asynccontextmanager
async def lifespan(app: FastAPI):
    path = os.getenv('ROYALBOOKS_DB_PATH', '/data/royalbooks.sqlite3')
    app.state.store = MetricsStore(path)
    app.state.jobs = JobStore(path)
    app.state.catalog_imports = CatalogImportService(path)
    app.state.watchlist = WatchlistStore(path)
    app.state.sessions = SessionStore(path)
    app.state.preferences = PlatformSettings(path)
    app.state.availability = Availability(path)
    await app.state.availability.start()
    sources = configured_providers()
    app.state.providers = {p.name: p for p in sources}
    app.state.hoster_jobs = HosterJobs(path)
    app.state.bookorbit = BookOrbitClient(BookOrbitConfig.environment())
    app.state.handoffs = Handoffs(path, BookOrbitConfig.environment().ingest)
    data = Path(path).parent
    ingest_path = BookOrbitConfig.environment().ingest
    app.state.search_client = httpx.AsyncClient(
        timeout=httpx.Timeout(5.0), follow_redirects=False, trust_env=False,
        limits=httpx.Limits(max_connections=8, max_keepalive_connections=8),
        event_hooks={'response': [bounded_response]},
        headers={'User-Agent': 'RoyalBooks/2.0 (https://github.com/TimeLance89/RoyalBooks)'})
    app.state.discovery = SearchCoordinator(sources, app.state.search_client, app.state.store)
    app.state.storefront = Storefront(path, app.state.search_client)
    app.state.storefront.availability = app.state.availability
    app.state.genres = Genres(app.state.storefront)
    app.state.artwork_slots = asyncio.Semaphore(3)
    storefront_task = (asyncio.create_task(app.state.storefront.run())
                       if os.getenv('ROYALBOOKS_STORE_REFRESH_ENABLED', '1') != '0' else None)
    task = None
    reconciliation_task = asyncio.create_task(app.state.handoffs.reconcile(app.state.bookorbit))
    hoster_task = asyncio.create_task(hoster_worker(app.state.hoster_jobs,app.state.providers['byte'],
        app.state.search_client,Path(ingest_path) if ingest_path else None)) if downloads_enabled() and os.getenv('ROYALBOOKS_HOSTER_ENABLED')=='1' and os.getenv('ROYALBOOKS_API_TOKEN') else None
    if downloads_enabled() and os.getenv('ROYALBOOKS_API_TOKEN'):
        task = asyncio.create_task(worker(app.state.jobs, data,
            Path(ingest_path) if ingest_path else None, app.state.providers.get('gutendex'),
            include_legacy=os.getenv('ROYALBOOKS_ENABLE_LEGACY_WORKER') == '1',availability=app.state.availability))
    try:
        yield
    finally:
        reconciliation_task.cancel()
        with suppress(asyncio.CancelledError):await reconciliation_task
        if hoster_task is not None:
            hoster_task.cancel()
            with suppress(asyncio.CancelledError):await hoster_task
        if storefront_task is not None:
            storefront_task.cancel()
            with suppress(asyncio.CancelledError):
                await storefront_task
        await app.state.genres.close()
        await app.state.discovery.close()
        await app.state.availability.close()
        await app.state.search_client.aclose()
        await app.state.bookorbit.close()
        await app.state.catalog_imports.close()
        if task is not None:
            task.cancel()
            with suppress(asyncio.CancelledError):
                await task


app = FastAPI(title='RoyalBooks', version='2.0.0', lifespan=lifespan)

# Normal book searches need no administrator token, but traffic is rate limited.
_search_windows: dict[str, list[float]] = {}
_search_rate_lock = asyncio.Lock()


async def _limit_public_search(client_ip: str) -> None:
    now = perf_counter()
    async with _search_rate_lock:
        for address in list(_search_windows):
            _search_windows[address] = [when for when in _search_windows[address] if now - when < 60]
            if not _search_windows[address]:
                del _search_windows[address]
        if len(_search_windows) > 1024 and client_ip not in _search_windows:
            raise HTTPException(status_code=429, detail='search_rate_limited')
        recent = _search_windows.setdefault(client_ip, [])
        if len(recent) >= 30:
            raise HTTPException(status_code=429, detail='search_rate_limited')
        recent.append(now)



@app.get('/')
@app.get('/connect')
@app.get('/discover')
@app.get('/library')
@app.get('/search')
@app.get('/authors')
@app.get('/genres')
@app.get('/downloads')
@app.get('/hosters')
@app.get('/watchlist')
@app.get('/settings')
@app.get('/settings/{tab}')
async def homepage():
    return FileResponse(Path(__file__).parent / 'static' / 'index.html',
                        headers={'Cache-Control': 'no-store', 'Referrer-Policy': 'no-referrer'})


@app.get('/api/health')
async def health():
    return {'status': 'ok', 'version': '2.0.0'}


@app.get('/api/store')
async def store_home():
    snapshot=await asyncio.to_thread(app.state.storefront.snapshot)
    preferences=getattr(app.state,'preferences',None)
    if preferences:
        selected=(await asyncio.to_thread(preferences.get))['sources']
        for shelf in snapshot['shelves']:
            if ('openlibrary' if shelf['id']=='trending' else 'libgen') not in selected:shelf['status']='disabled'
            shelf['items']=[item for item in shelf['items'] if item['source'] in selected]
    return JSONResponse(snapshot,
                        headers={'Cache-Control':'no-store'})


@app.get('/api/genres')
async def genre_list():
    return {'genres':genre_manifest()}


@app.get('/api/genres/{slug}')
async def genre_books(slug: str,request: Request,language: str = Query(default='de',pattern='^(de|en|all)$'),page: int = Query(default=1,ge=1,le=40)):
    if slug not in GENRES:raise HTTPException(status_code=404,detail='unknown_genre')
    await _limit_public_search('genre:'+(request.client.host if request.client else 'unknown'))
    return await app.state.genres.browse(slug,language,page)


@app.get('/api/catalog-cover/{md5}')
async def catalog_cover(md5: str = ApiPath(pattern=r'^[a-f0-9]{32}$')):
    try:
        async with app.state.artwork_slots:
            content = await asyncio.wait_for(fetch_artwork(app.state.storefront.path, md5,
                                            app.state.search_client),timeout=6)
    except (httpx.HTTPError, asyncio.TimeoutError):
        content = None
    if not content:
        raise HTTPException(status_code=404, detail='cover_unavailable')
    return Response(content,media_type='image/jpeg',headers={'Cache-Control':'public, max-age=86400',
                                                            'X-Content-Type-Options':'nosniff'})


@app.get('/api/book-preview')
async def book_preview(request: Request,
                       isbn: str | None = Query(default=None, pattern=r'^97[89][0-9]{10}$'),
                       work: str | None = Query(default=None, pattern=r'^OL[0-9]{1,12}W$')):
    await _limit_public_search('preview:' + (request.client.host if request.client else 'unknown'))
    if bool(isbn) == bool(work):
        raise HTTPException(status_code=422, detail='provide_isbn_or_work')
    return JSONResponse(await app.state.storefront.preview(isbn,work),
                        headers={'Cache-Control':'no-store'})


@app.get('/api/providers')
async def providers():
    coordinator = getattr(app.state, 'discovery', None)
    return {'active_indexers': [], 'indexer_configuration_error': None,
            'search_mode': 'integrated_providers',
            'catalog': await asyncio.to_thread(catalog_status, app.state.jobs.path),
            'format_catalog': await asyncio.to_thread(format_status, app.state.jobs.path),
            'providers': [{'name': p.name, 'purpose': p.purpose,
                           **(coordinator.health.get(p.name, {}) if coordinator else {})}
                          for p in app.state.providers.values()],
            'source_profiles': profiles_for(app.state.providers.keys()),
            'recent_metrics': await asyncio.to_thread(app.state.store.stats)}


async def _run(provider: Provider, client: httpx.AsyncClient, q: str, language: str | None, limit: int):
    start = perf_counter()
    try:
        # Gutendex can be access-blocked from NAS networks; do not hold the whole
        # search page for nearly 18 seconds while the other sources already responded.
        cap = 6.0 if provider.name == 'gutendex' else 18.0
        items = await asyncio.wait_for(provider.search(client, q, language, limit), timeout=cap)
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


async def _search(q: str, language: str, limit: int, mode='all',page=1,selected=None):
    requested = None if language == 'all' else language
    coordinator = getattr(app.state, 'discovery', None)
    if coordinator:
        try:
            items, states, analyzed = await coordinator.search(q, requested, limit,mode,page,selected)
        except Exception as exc:
            if str(exc) == 'search_busy':
                raise HTTPException(status_code=503, detail='search_busy') from exc
            raise
    else:
        # Also supports embedders which explicitly supply their own registry.
        analyzed = analyze_query(q)
        async with httpx.AsyncClient(timeout=5, follow_redirects=False, trust_env=False) as client:
            results = await asyncio.gather(*[_run(p, client, analyzed.text, requested, limit)
                                            for p in app.state.providers.values()])
        items = [book for batch, _ in results for book in batch]
        states = [state for _, state in results]
    fallback_ids=set();resolved_title=None
    if coordinator and analyzed.isbn and page==1 and not any(item.as_dict()['download_supported'] for item in items):
        reference=next((book for book in items if book.authors and any(
            canonical_isbn(number)==analyzed.isbn for number in [*book.isbns,book.isbn13] if isinstance(number,str))),None)
        if reference:
            names=[p.name for p in coordinator.providers if p.purpose!='metadata' and (not selected or p.name in selected)]
            if names:
                query=(reference.title[:78]+' '+reference.authors[0][:38])[:120]
                alternatives,extra_states,_=await coordinator.search(query,requested,25,'all',1,names)
                accepted=[book for book in alternatives if book.as_dict()['download_supported'] and same_work(reference,book)]
                if accepted:
                    items.extend(accepted);fallback_ids={(book.source,book.source_id) for book in accepted};resolved_title=reference.title
                    states.extend({**state,'work_fallback':True} for state in extra_states)
    serialized=[{**item.as_dict(),**({'edition_match':'same_work_other_edition'} if (item.source,item.source_id) in fallback_ids else {})} for item in items]
    if getattr(app.state,'store',None):
        await asyncio.to_thread(remember,app.state.store.path,serialized)
    return {'query': analyzed.text, 'language': language, 'mode': 'integrated_providers',
            'search_mode':mode,'page':page,'has_more':any(s.get('has_more',s['results']>=min(limit,25)) for s in states),
            'query_analysis': {'title': analyzed.title, 'author': analyzed.author,
                               'isbn': analyzed.isbn, 'variants': analyzed.variants,
                               'original_query': analyzed.original_text},
            'resolved_work':resolved_title,
            'items': serialized,
            'match_groups': match_groups(items), 'display_groups': display_groups(items, analyzed.text),
            'search_evidence': search_evidence(items), 'sources': states}


def same_work(reference, candidate):
    def title(value):
        words=normalize(value.split(':',1)[0]).split()
        while words and words[-1] in {'roman','novel','ebook','epub','thriller','psychothriller'}:words.pop()
        return ' '.join(words)
    authors=lambda book:{normalize_person(re.sub(r'\[[^]]*\]','',author)) for author in book.authors}
    return bool(title(reference.title) and title(reference.title)==title(candidate.title)
                and authors(reference)&authors(candidate) and reference.language==candidate.language)


@app.get('/api/search')
@app.get('/api/books/search')
async def books_search(
    request: Request,
    q: str = Query(min_length=2, max_length=120),
    language: str = Query(default='de', pattern='^(de|en|all)$'),
    limit: int = Query(default=30, ge=1, le=60),
    mode: Literal['all','title','author','isbn'] = 'all',
    page: int = Query(default=1,ge=1,le=40),
):
    await _limit_public_search(request.client.host if request.client else 'unknown')
    if len(q.strip()) < 2:
        raise HTTPException(status_code=422, detail='query_too_short')
    analyzed=analyze_query(q)
    if mode=='isbn' and not analyzed.isbn:
        raise HTTPException(status_code=422,detail='invalid_book_isbn_ean')
    preferences=getattr(app.state,'preferences',None)
    selected=(await asyncio.to_thread(preferences.get))['sources'] if preferences else None
    return await _search(q, language, limit,mode,page,selected)


@app.get('/api/indexers/search')
async def indexer_search(
    request: Request,
    q: str = Query(min_length=2, max_length=120),
    language: str = Query(default='de', pattern='^(de|en|all)$'),
    limit: int = Query(default=30, ge=1, le=40),
):
    await _limit_public_search(request.client.host if request.client else 'unknown')
    try:
        result = await search_indexers(q, None if language == 'all' else language, limit)
    except (OSError, ValueError):
        raise HTTPException(status_code=503, detail='invalid_indexer_configuration')
    return {'query': q, 'language': language, **result,
            'mode': 'read_only',
            'notice': 'Indexer listings are unverified.'}


class QueueRequest(BaseModel):
    provider: str = 'gutendex'
    source_id: int = Field(gt=0, le=9223372036854775807)
    rights_confirmed: bool = False


def _guard(token: str | None) -> None:
    try:
        require_token(os.getenv('ROYALBOOKS_API_TOKEN'), token)
    except PermissionError as exc:
        status = 503 if str(exc) == 'acquisition_disabled_without_token' else 401
        raise HTTPException(status_code=status, detail=str(exc)) from exc


def _same_origin_write(request: Request):
    if request.headers.get('X-RoyalBooks-Client') != 'web':
        raise HTTPException(status_code=403, detail='same_origin_required')
    origin = request.headers.get('origin')
    if origin and origin.rstrip('/') != str(request.base_url).rstrip('/'):
        raise HTTPException(status_code=403, detail='same_origin_required')
    if request.headers.get('sec-fetch-site') == 'cross-site':
        raise HTTPException(status_code=403, detail='same_origin_required')


def _network_access(request: Request):
    """Explicit operator allowlist; forwarded headers never grant access."""
    if not os.getenv('ROYALBOOKS_API_TOKEN') or not request.client:
        return False
    try:
        peer = ipaddress.ip_address(request.client.host)
        networks = [ipaddress.ip_network(value.strip()) for value in
                    os.getenv('ROYALBOOKS_TRUSTED_NETWORKS', '').split(',') if value.strip()]
        return any(peer in network for network in networks)
    except ValueError:
        return False


def admin_access(request: Request, x_api_key: str | None = Header(default=None)):
    if x_api_key:
        _guard(x_api_key)
        return
    sessions = getattr(request.app.state, 'sessions', None)
    key = os.getenv('ROYALBOOKS_API_TOKEN', '')
    if _network_access(request) or (sessions and sessions.authenticated(request.cookies.get(COOKIE), key)):
        if request.method not in {'GET', 'HEAD', 'OPTIONS'}:
            _same_origin_write(request)
        return
    _guard(None)


class PreferenceUpdate(BaseModel):
    language: Literal['de','en','all'] | None = None
    available_only: bool | None = None
    result_limit: Literal[25,30,60] | None = None
    sources: list[str] | None = Field(default=None,min_length=1,max_length=11)

    @field_validator('sources')
    @classmethod
    def valid_sources(cls,values):
        from royalbooks.source_profiles import PROFILES
        if values is not None and any(value not in PROFILES for value in values):raise ValueError('unknown_source')
        return list(dict.fromkeys(values)) if values is not None else None


@app.get('/api/settings')
async def platform_preferences(_admin: None = Depends(admin_access)):
    return {'preferences':await asyncio.to_thread(app.state.preferences.get),
            'connections':{'backend':'bookorbit','library':bool(BookOrbitConfig.environment().url),
                           'ingest':bool(BookOrbitConfig.environment().ingest),
                           'downloads':downloads_enabled()}}


@app.put('/api/settings')
async def update_preferences(body: PreferenceUpdate,_admin: None = Depends(admin_access)):
    return {'preferences':await asyncio.to_thread(app.state.preferences.save,body.model_dump(exclude_none=True))}


class AvailabilityItem(BaseModel):
    source: Literal['webshare','libgen','memoryoftheworld','gutendex','gutenberg_catalog','wikisource','standardebooks']
    record_id: int = Field(gt=0,le=9007199254740991)


class AvailabilityRequest(BaseModel):
    items: list[AvailabilityItem] = Field(max_length=96)
    context: Literal['search','store'] = 'search'


@app.post('/api/availability')
async def available_files(body: AvailabilityRequest,request: Request):
    _same_origin_write(request)
    await _limit_public_search('availability:'+(request.client.host if request.client else 'unknown'))
    selected=(await asyncio.to_thread(app.state.preferences.get))['sources']
    records=[(item.source,item.record_id) for item in body.items if item.source in selected]
    return {'items':await app.state.availability.request(records,priority=5 if body.context=='store' else 0),
            'notice':'Reachable catalog-listed ZIP; complete EPUB integrity is checked during download.'}


class LoginRequest(BaseModel):
    token: str = Field(min_length=1, max_length=512)


@app.get('/api/library')
async def library_books(q: str = Query(default='', max_length=120),
                        sort: Literal['recent', 'title', 'author'] = 'recent',
                        limit: int = Query(default=60, ge=1, le=100),
                        offset: int = Query(default=0, ge=0, le=100000),
                        _admin: None = Depends(admin_access)):
    try:
        if os.getenv('ROYALBOOKS_LIBRARY_BACKEND') == 'calibre-readonly':
            result = await asyncio.to_thread(bookshelf, os.getenv('ROYALBOOKS_CWA_LIBRARY_PATH'), q, sort, limit, offset)
        else:
            result = await orbit_client().shelf(q, sort, limit, offset)
        return JSONResponse(result, headers={'Cache-Control':'no-store'})
    except (LibraryUnavailable, BookOrbitError) as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc


@app.get('/api/library/{book_id}/{kind}')
async def library_asset(book_id: int = ApiPath(gt=0), kind: Literal['cover', 'epub'] = 'cover',
                        _admin: None = Depends(admin_access)):
    if os.getenv('ROYALBOOKS_LIBRARY_BACKEND') != 'calibre-readonly':
        try:
            payload = await orbit_client().asset(book_id, kind)
            return Response(payload, media_type='image/jpeg' if kind == 'cover' else 'application/epub+zip',
                            headers={'Cache-Control':'private, no-store','X-Content-Type-Options':'nosniff',
                                     **({'Content-Disposition':f'attachment; filename="bookorbit-{book_id}.epub"'} if kind=='epub' else {})})
        except BookOrbitError as exc:
            raise HTTPException(status_code=404 if str(exc)=='bookorbit_http_404' else 503, detail=str(exc)) from exc
    try:
        file = await asyncio.to_thread(library_file, os.getenv('ROYALBOOKS_CWA_LIBRARY_PATH'), book_id, kind)
    except LibraryUnavailable as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    if file is None:
        raise HTTPException(status_code=404, detail='library_file_not_found')
    return FileResponse(file, media_type='image/jpeg' if kind == 'cover' else 'application/epub+zip',
                        filename=file.name if kind == 'epub' else None,
                        headers={'Cache-Control':'private, no-store', 'X-Content-Type-Options':'nosniff'})


class DeviceLinkRequest(BaseModel):
    code: str = Field(min_length=32, max_length=128)


def _session_response(request: Request, value: str):
    response = JSONResponse({'authenticated': True}, headers={'Cache-Control':'no-store'})
    response.set_cookie(COOKIE, value, max_age=SESSION_SECONDS, httponly=True,
                        secure=request.url.scheme == 'https', samesite='strict', path='/')
    return response


@app.get('/api/auth/status')
async def auth_status(request: Request):
    if _network_access(request):
        return JSONResponse({'authenticated': True, 'network_access': True},
                            headers={'Cache-Control':'no-store'})
    sessions = getattr(request.app.state, 'sessions', None)
    valid = bool(sessions and await asyncio.to_thread(sessions.authenticated,
        request.cookies.get(COOKIE), os.getenv('ROYALBOOKS_API_TOKEN', '')))
    return JSONResponse({'authenticated': valid}, headers={'Cache-Control':'no-store'})


@app.post('/api/auth/login')
async def login(body: LoginRequest, request: Request):
    _same_origin_write(request)
    await _limit_public_search('auth:' + (request.client.host if request.client else 'unknown'))
    _guard(body.token)
    value = await asyncio.to_thread(request.app.state.sessions.create_session,
                                   os.environ['ROYALBOOKS_API_TOKEN'])
    return _session_response(request, value)


@app.post('/api/auth/connect')
async def connect_device(body: DeviceLinkRequest, request: Request):
    _same_origin_write(request)
    await _limit_public_search('auth:' + (request.client.host if request.client else 'unknown'))
    key = os.getenv('ROYALBOOKS_API_TOKEN', '')
    # Reopening a bookmark or an already-consumed link must not hide a valid
    # login on this device. Do not renew its expiry or consume another link.
    if await asyncio.to_thread(request.app.state.sessions.authenticated,
                               request.cookies.get(COOKIE), key):
        return JSONResponse({'authenticated': True}, headers={'Cache-Control':'no-store'})
    if not await asyncio.to_thread(request.app.state.sessions.consume_device_link, body.code, key):
        raise HTTPException(status_code=401, detail='device_link_expired_or_used')
    value = await asyncio.to_thread(request.app.state.sessions.create_session, key)
    return _session_response(request, value)


@app.post('/api/auth/logout')
async def logout(request: Request):
    _same_origin_write(request)
    await asyncio.to_thread(request.app.state.sessions.logout, request.cookies.get(COOKIE))
    response = JSONResponse({'authenticated': False}, headers={'Cache-Control':'no-store'})
    response.delete_cookie(COOKIE, path='/', httponly=True, samesite='strict')
    return response


@app.post('/api/jobs', status_code=202)
async def enqueue(request: QueueRequest, _admin: None = Depends(admin_access)):
    if not downloads_enabled():
        raise HTTPException(status_code=503, detail='downloads_disabled')
    if request.provider not in {'gutendex', 'gutenberg_catalog', *SOURCES}:
        raise HTTPException(status_code=400, detail='unsupported_download_provider')
    if not request.rights_confirmed:
        raise HTTPException(status_code=400, detail='local_rights_confirmation_required')
    if request.provider == 'gutenberg_catalog' and not await asyncio.to_thread(
        lookup_epub, app.state.jobs.path, request.source_id
    ):
        raise HTTPException(status_code=409, detail='official_epub_metadata_missing')
    if request.provider in SOURCES and not await asyncio.to_thread(
        lookup_direct_epub, app.state.jobs.path, request.provider, request.source_id
    ):
        raise HTTPException(status_code=409, detail='publisher_epub_metadata_missing_or_expired')
    if request.provider == 'webshare':
        from royalbooks.webshare_source import cached
        if not await asyncio.to_thread(cached, app.state.jobs.path, request.source_id):
            raise HTTPException(status_code=409, detail='verified_epub_cache_expired_search_again')
    return {'job': await asyncio.to_thread(app.state.jobs.add, request.source_id, request.provider, v2_authorized=True)}


class CatalogImportRequest(BaseModel):
    source: Literal["csv", "rdf"]


@app.get('/api/catalog/imports')
async def catalog_import_history(_admin: None = Depends(admin_access)):
    """Authenticated status of explicitly requested first-party feed imports."""
    return {'imports': await asyncio.to_thread(app.state.catalog_imports.history),
            'notice': 'Catalog links are not verified downloads or copyright clearance.'}


@app.post('/api/catalog/imports', status_code=202)
async def request_catalog_import(
    request: CatalogImportRequest,
    _admin: None = Depends(admin_access),
):
    try:
        return {'import': app.state.catalog_imports.request(request.source)}
    except CatalogImportBusy as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


class WatchlistRequest(BaseModel):
    source: Literal["webshare", "byte", "gutendex", "gutenberg_catalog", "openlibrary", "dnb", "google_books", "libgen", "wikisource", "standardebooks", "memoryoftheworld"]
    source_id: str = Field(min_length=1, max_length=100, pattern=r"^[A-Za-z0-9_-]+$")
    title: str = Field(min_length=2, max_length=250)
    authors: list[str] = Field(default_factory=list, max_length=8)
    language: str | None = Field(default=None, max_length=3, pattern=r"^[a-z]{2,3}$")
    info_url: str | None = Field(default=None, max_length=500, pattern=r"^https://")

    @field_validator("authors")
    @classmethod
    def check_authors(cls, value: list[str]) -> list[str]:
        if any(not author.strip() or len(author) > 120 for author in value):
            raise ValueError("invalid_author")
        return value


@app.get("/api/watchlist")
async def list_watchlist(_admin: None = Depends(admin_access)):
    return {"items": await asyncio.to_thread(app.state.watchlist.list),
            "notice": "A saved book is not a download request."}


@app.post("/api/watchlist", status_code=201)
async def save_to_watchlist(book: WatchlistRequest, _admin: None = Depends(admin_access)):
    try:
        item = await asyncio.to_thread(app.state.watchlist.add, **book.model_dump())
    except WatchlistFull as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return {"item": item}


@app.delete("/api/watchlist/{item_id}")
async def delete_watchlist_item(
    item_id: str = ApiPath(min_length=32, max_length=32, pattern=r"^[0-9a-f]{32}$"),
    _admin: None = Depends(admin_access),
):
    removed = await asyncio.to_thread(app.state.watchlist.remove, item_id)
    if not removed:
        raise HTTPException(status_code=404, detail="item_not_found")
    return {"removed": True}


def orbit_client():
    client = getattr(app.state, 'bookorbit', None)
    if client is None:
        client = app.state.bookorbit = BookOrbitClient(BookOrbitConfig.environment())
    return client


@app.get('/api/bookorbit/status')
async def bookorbit_status(_admin: None = Depends(admin_access)):
    return await orbit_client().connection()


@app.post('/api/bookorbit/test')
async def bookorbit_test(_admin: None = Depends(admin_access)):
    config = BookOrbitConfig.environment()
    connection = await orbit_client().connection()
    storage = await asyncio.to_thread(diagnose,
        data_path=str(Path(app.state.jobs.path).parent), ingest_path=config.ingest or None,
        library_path=config.library or None, write_test=True)
    return {'connection':connection,'storage':storage,
            'notice':'Prüfdateien enden auf .part und werden entfernt. Kein Buchimport wird ausgelöst.'}


@app.get('/api/preflight')
async def preflight_status(_admin: None = Depends(admin_access)):
    """Authenticated BookOrbit storage checks; no credentials disclosed."""
    root = str(Path(os.getenv("ROYALBOOKS_DB_PATH", "/data/royalbooks.sqlite3")).parent)
    return await asyncio.to_thread(
        diagnose,
        data_path=root,
        ingest_path=BookOrbitConfig.environment().ingest or None,
        library_path=BookOrbitConfig.environment().library or None,
        api_token=os.getenv("ROYALBOOKS_API_TOKEN") or None,
    )


@app.get('/api/jobs')
async def list_jobs(_admin: None = Depends(admin_access)):
    ingest_path = BookOrbitConfig.environment().ingest
    ingest = Path(ingest_path) if ingest_path else None
    staging = Path(app.state.jobs.path).parent / 'staging'
    records = await asyncio.to_thread(app.state.jobs.list)
    records = await asyncio.to_thread(apply_labels,app.state.jobs.path,records)
    for job in records:
        journal = Handoffs(app.state.jobs.path, ingest)
        job['library_transfer'] = await asyncio.to_thread(journal.describe, job)
        job['handoff_observation'] = await asyncio.to_thread(observe_handoff, job, staging, ingest) if job['library_transfer']['backend']=='legacy' else None
    return {'jobs': records, 'legacy_worker_enabled': os.getenv('ROYALBOOKS_ENABLE_LEGACY_WORKER') == '1',
            'downloads_enabled': downloads_enabled(),
            'ingest_configured': bool(ingest_path),
            'library_configured': bool(BookOrbitConfig.environment().url),
            'library_backend': 'bookorbit',
            'handoff_notice': 'A disappearing Book Dock file does not confirm a successful BookOrbit import.'}


@app.get('/api/hosters/catalog')
async def hoster_catalog(request:Request,q:str=Query(default='',max_length=140),page:int=Query(default=1,ge=1,le=2000)):
    await _limit_public_search(request.client.host if request.client else 'unknown')
    provider=app.state.providers.get('byte')
    if not provider:raise HTTPException(status_code=503,detail='byte_not_configured')
    try:
        items,more=await provider.catalog(app.state.search_client,q.strip(),page)
        return {'items':items,'has_more':more,'page':page,
                'notice':'Source listings; EPUB format, language and hoster availability are not verified.'}
    except ProviderError as exc:
        raise HTTPException(status_code=502,detail=str(exc)) from exc


@app.get('/api/hosters')
async def hoster_status(_admin:None=Depends(admin_access)):
    store=app.state.hoster_jobs
    records=await asyncio.to_thread(store.list)
    journal=Handoffs(store.path,BookOrbitConfig.environment().ingest)
    for job in records:job['library_transfer']=await asyncio.to_thread(journal.describe,job)
    return {'configured':store.ready(),'jobs':records,
            'engine':await asyncio.to_thread(engine_status,store.root),
            'control_url':os.getenv('ROYALBOOKS_HOSTER_CONTROL_URL',''),
            'notice':'Free hosters may require waiting or manual captchas in the download engine.'}


class HosterRequest(BaseModel):
    source_id:int=Field(gt=0,le=9999999999)
    rights_confirmed:bool=False


@app.post('/api/hosters/jobs',status_code=202)
async def enqueue_hoster(body:HosterRequest,_admin:None=Depends(admin_access)):
    if not downloads_enabled():raise HTTPException(status_code=503,detail='downloads_disabled')
    if not body.rights_confirmed:raise HTTPException(status_code=400,detail='local_rights_confirmation_required')
    try:
        item=await asyncio.to_thread(app.state.providers['byte'].lookup,body.source_id)
        return {'job':await asyncio.to_thread(app.state.hoster_jobs.add,item)}
    except ProviderError as exc:
        raise HTTPException(status_code=409,detail=str(exc)) from exc


class ManualHosterLinks(BaseModel):
    links:list[str]=Field(min_length=1,max_length=8)
    rights_confirmed:bool=False


@app.post('/api/hosters/jobs/{job_id}/links',status_code=202)
async def hoster_manual_links(body:ManualHosterLinks,
    job_id:str=ApiPath(pattern=r'^[a-f0-9]{32}$'),_admin:None=Depends(admin_access)):
    if not downloads_enabled():raise HTTPException(status_code=503,detail='downloads_disabled')
    if not body.rights_confirmed:raise HTTPException(status_code=400,detail='local_rights_confirmation_required')
    try:
        await asyncio.to_thread(app.state.hoster_jobs.manual_links,job_id,body.links)
        return {'accepted':True}
    except ProviderError as exc:
        raise HTTPException(status_code=409,detail=str(exc)) from exc


class RetryRequest(BaseModel):
    rights_confirmed: bool = False


@app.post('/api/jobs/{job_id}/retry', status_code=202)
async def retry_job(
    request: RetryRequest,
    job_id: str = ApiPath(min_length=32, max_length=32, pattern=r'^[0-9a-f]{32}$'),
    _admin: None = Depends(admin_access),
):
    if not downloads_enabled():
        raise HTTPException(status_code=503, detail='downloads_disabled')
    if not request.rights_confirmed:
        raise HTTPException(status_code=400, detail='local_rights_confirmation_required')
    try:
        updated = await asyncio.to_thread(app.state.jobs.retry_failed, job_id, v2_authorized=True)
        return {'job': updated}
    except JobStateError as exc:
        code = 404 if str(exc) == 'job_not_found' else 409
        raise HTTPException(status_code=code, detail=str(exc)) from exc


@app.get('/api/jobs/{job_id}/library-check')
async def library_check(
    job_id: str = ApiPath(min_length=32, max_length=32, pattern=r'^[0-9a-f]{32}$'),
    _admin: None = Depends(admin_access),
):
    job = await asyncio.to_thread(app.state.jobs.get, job_id)
    if job is None:
        raise HTTPException(status_code=404, detail='job_not_found')
    journal = Handoffs(app.state.jobs.path, BookOrbitConfig.environment().ingest)
    verification = await journal.verify(job, orbit_client())
    return {'job_id': job_id, 'verification': verification}
