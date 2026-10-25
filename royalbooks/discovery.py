"""Bounded, independent providers; cached catalog claims never become verified files."""
from __future__ import annotations

import asyncio
import re
import unicodedata
from collections import OrderedDict
from dataclasses import dataclass
from time import monotonic
from datetime import datetime, timezone

import httpx

from royalbooks.providers import Book, Provider, ProviderError, canonical_isbn


async def bounded_response(response: httpx.Response):
    """Bound decompressed catalog bodies before adapters load JSON/XML/HTML."""
    data = bytearray()
    async for chunk in response.aiter_bytes(65536):
        data.extend(chunk)
        if len(data) > 2 * 1024 * 1024:
            await response.aclose()
            raise ProviderError('catalog_response_too_large')
    response._content = bytes(data)


@dataclass(frozen=True)
class SearchQuery:
    text: str
    title: str | None
    author: str | None
    isbn: str | None
    variants: tuple[str, ...]


def analyze_query(value: str) -> SearchQuery:
    text = ' '.join(unicodedata.normalize('NFKC', value).split())
    # Only a whole ISBN query is transformed; numbers inside titles stay intact.
    candidate = re.sub(r'[\s-]', '', re.sub(r'^isbn(?:-1[03])?:?\s*', '', text, flags=re.I)).upper()
    number = canonical_isbn(candidate)
    if number:
        return SearchQuery(number, None, None, number, (number,))
    parts = re.split(r'\s+author:\s*', text, maxsplit=1, flags=re.I)
    title, author = (parts[0], parts[1]) if len(parts) == 2 else (None, None)
    if title and author:
        text = title + ' author:' + author
    variants = tuple(dict.fromkeys([text, *([title] if title else [])]))
    return SearchQuery(text, title, author, None, variants)


class SearchCoordinator:
    """One process-wide client and at most six concurrent provider operations.

    Deadlines include lock/connection waits. At most 32 distinct requests can be
    in flight. Identical searches share work; disconnecting a caller does not
    cancel work needed by another. Shutdown cancels all outstanding requests.
    """
    def __init__(self, providers: list[Provider], client: httpx.AsyncClient,
                 store=None, *, timeout: float = 8.0, cache_seconds: float = 120):
        self.providers = providers
        self.client = client
        self.store = store
        self.timeout = timeout
        self.cache_seconds = cache_seconds
        self.slots = asyncio.Semaphore(6)
        self.health: dict[str, dict] = {}
        self.cache: OrderedDict = OrderedDict()
        self.inflight: dict[tuple, asyncio.Task] = {}
        self.cooldowns: dict[str, float] = {}

    async def close(self):
        pending = list(self.inflight.values())
        for task in pending:
            task.cancel()
        await asyncio.gather(*pending, return_exceptions=True)

    async def _run(self, provider, query, language, limit):
        start = monotonic()
        if language and provider.supported_languages and language not in provider.supported_languages:
            return [], dict(provider=provider.name, status='skipped', results=0,
                            duration_ms=0, error=None, health='not_applicable')
        books, error = [], None
        try:
            if self.cooldowns.get(provider.name, 0) > monotonic():
                raise ProviderError('source_cooldown')
            async with asyncio.timeout(self.timeout):
                async with self.slots:
                    provider_query = query if provider.name == 'dnb' else query.replace(' author:', ' ')
                    books = await provider.search(self.client, provider_query, language, limit)
            status = 'found' if books else 'empty'
        except Exception as exc:
            status = 'error'
            # Only controlled codes, never exception strings with URLs/secrets.
            code = str(exc) if isinstance(exc, ProviderError) else type(exc).__name__
            error = code if re.fullmatch(r'[A-Za-z0-9_]{1,80}', code) else 'provider_error'
            if re.search(r'(?:^|_)(403|451|429)(?:_|$)', error):
                self.cooldowns[provider.name] = monotonic() + 1800
        duration = int((monotonic() - start) * 1000)
        state = dict(provider=provider.name, status=status, results=len(books),
                     duration_ms=duration, error=error,
                     health='unavailable' if error else ('slow' if duration > 4000 else 'available'))
        self.health[provider.name] = {**state, 'checked_at': datetime.now(timezone.utc).isoformat()}
        if self.store:
            await asyncio.to_thread(self.store.record, provider.name, status, len(books), duration, error)
        return books, state

    async def _search(self, key):
        query, language, limit = key
        # Healthy sources acquire slots first; a failing source remains independent.
        providers = sorted(self.providers, key=lambda p: self.health.get(p.name, {}).get('health') == 'unavailable')
        results = await asyncio.gather(*(self._run(p, query, language, limit) for p in providers))
        books = [book for batch, _ in results for book in batch]
        states = [state for _, state in results]
        ttl = self.cache_seconds if all(s['status'] != 'error' for s in states) else min(10, self.cache_seconds)
        self.cache[key] = (monotonic() + ttl, books, states)
        self.cache.move_to_end(key)
        while len(self.cache) > 128:
            self.cache.popitem(last=False)
        return books, states

    async def search(self, value, language, limit):
        query = analyze_query(value)
        key = (query.text, language, limit)
        cached = self.cache.get(key)
        if cached and cached[0] > monotonic():
            return cached[1], [{**state, 'cached': True} for state in cached[2]], query
        task = self.inflight.get(key)
        if task is None:
            if len(self.inflight) >= 32:
                raise ProviderError('search_busy')
            task = asyncio.create_task(self._search(key))
            self.inflight[key] = task
            task.add_done_callback(lambda finished: self.inflight.pop(key, None))
        books, states = await asyncio.shield(task)
        return books, states, query
