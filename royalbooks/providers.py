"""Independent catalog adapters. A catalog record is never proof of a legal download."""
from __future__ import annotations

import asyncio
from dataclasses import asdict, dataclass
import re
from urllib.parse import quote
from time import monotonic
from typing import Any

import httpx


class ProviderError(Exception):
    """One source failed, without stopping other sources."""


@dataclass(frozen=True)
class Book:
    source: str
    source_id: str
    title: str
    authors: list[str]
    language: str | None
    year: int | None
    cover_url: str | None
    info_url: str
    epub_url: str | None = None
    availability: str = "metadata_only"
    isbn13: str | None = None
    isbns: tuple[str, ...] = ()
    formats: tuple[str, ...] = ()
    description: str | None = None
    language_evidence: str = "catalog_declared"
    record_kind: str = "edition"
    acquisition_id: int | None = None

    def as_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data['isbns'] = list(dict.fromkeys((*self.isbns, *([self.isbn13] if self.isbn13 else []))))
        data['formats'] = list(self.formats or (('EPUB',) if self.epub_url else ()))
        data['file_evidence'] = ('listed_unverified' if self.epub_url or self.formats else 'metadata_only')
        data['language_evidence'] = self.language_evidence if self.language else 'ambiguous_or_unknown'
        data['download_supported'] = bool(self.epub_url) and self.source in {
            'gutendex', 'gutenberg_catalog', 'wikisource', 'standardebooks', 'memoryoftheworld'}
        if self.source == 'libgen' and self.acquisition_id and self.formats == ('EPUB',):
            data['download_supported'] = True
        return data


class Provider:
    name = "unknown"
    purpose = "metadata"
    min_interval = 1.1
    supported_languages = None

    def __init__(self) -> None:
        self._lock = asyncio.Lock()
        self._last_request = 0.0
        # Honor an origin's access refusal rather than repeatedly hammering it.
        self._blocked_until = 0.0

    def cooldown_remaining(self) -> float:
        return max(0.0, self._blocked_until - monotonic())

    async def get_json(self, client: httpx.AsyncClient, url: str, params: dict[str, Any]) -> dict[str, Any]:
        # Serialized per source; Open Library explicitly limits anonymous clients to 1 req/s.
        async with self._lock:
            if self.cooldown_remaining():
                raise ProviderError("source_cooldown_http_403")
            delay = max(0, self.min_interval - (monotonic() - self._last_request))
            if delay:
                await asyncio.sleep(delay)
            for attempt in range(3):
                self._last_request = monotonic()
                try:
                    response = await client.get(url, params=params)
                    if response.status_code in {429, 500, 502, 503, 504} and attempt < 2:
                        await asyncio.sleep(min(2.0, 0.5 * 2**attempt))
                        continue
                    response.raise_for_status()
                    payload = response.json()
                    if not isinstance(payload, dict):
                        raise ProviderError("invalid_json_shape")
                    return payload
                except (httpx.RequestError, ValueError) as exc:
                    if attempt == 2:
                        raise ProviderError(type(exc).__name__) from exc
                    await asyncio.sleep(min(2.0, 0.5 * 2**attempt))
                except httpx.HTTPStatusError as exc:
                    if exc.response.status_code in (403, 451):
                        self._blocked_until = monotonic() + 1800.0
                    raise ProviderError(f"http_{exc.response.status_code}") from exc
            raise ProviderError("retry_exhausted")

    async def search(self, client: httpx.AsyncClient, query: str, language: str | None, limit: int) -> list[Book]:
        raise NotImplementedError


def _language(values: object) -> str | None:
    mapping = {"ger": "de", "deu": "de", "de": "de", "eng": "en", "en": "en"}
    if not isinstance(values, list) or not values:
        return None
    languages = {mapping.get(v, v) for v in values if isinstance(v, str)}
    return next(iter(languages)) if len(languages) == 1 else None


def _str_list(values: object) -> list[str]:
    return [v for v in values if isinstance(v, str)] if isinstance(values, list) else []


def isbn13(identifiers: object) -> str | None:
    if not isinstance(identifiers, list):
        return None
    for entry in identifiers:
        number = entry.get('identifier') if isinstance(entry, dict) else entry
        if not isinstance(number, str) or not re.fullmatch(r'97[89]\d{10}', number):
            continue
        digits = [int(x) for x in number]
        if (sum(d * (1 if i % 2 == 0 else 3) for i, d in enumerate(digits[:-1])) + digits[-1]) % 10 == 0:
            return number
    return None


def valid_isbn10(value: str) -> bool:
    if not re.fullmatch(r'\d{9}[\dX]', value):
        return False
    return sum((10-i) * (10 if digit == 'X' else int(digit)) for i, digit in enumerate(value)) % 11 == 0


def canonical_isbn(value: str) -> str | None:
    if number := isbn13([value]):
        return number
    if valid_isbn10(value):
        prefix = '978' + value[:9]
        check = (-sum(int(d) * (1 if i % 2 == 0 else 3) for i, d in enumerate(prefix))) % 10
        return prefix + str(check)
    return None


class GoogleBooks(Provider):
    name = 'google_books'
    purpose = 'metadata'
    min_interval = 1.0

    def __init__(self, api_key: str):
        super().__init__()
        self.api_key = api_key

    async def search(self, client: httpx.AsyncClient, query: str, language: str | None, limit: int) -> list[Book]:
        params: dict[str, Any] = {'q': query, 'maxResults': min(limit, 40), 'printType': 'books', 'key': self.api_key}
        if language:
            params['langRestrict'] = language
        data = await self.get_json(client, 'https://www.googleapis.com/books/v1/volumes', params)
        books = []
        for item in data.get('items', [])[:limit]:
            if not isinstance(item, dict):
                continue
            source_id = item.get('id')
            info = item.get('volumeInfo')
            if not isinstance(info, dict) or not isinstance(source_id, str) or not re.fullmatch(r'[A-Za-z0-9_-]{1,64}', source_id):
                continue
            title = info.get('title')
            if not isinstance(title, str) or not title.strip():
                continue
            date = info.get('publishedDate', '')
            year = int(date[:4]) if isinstance(date, str) and re.match(r'^\d{4}', date) else None
            language_tag = info.get('language') if isinstance(info.get('language'), str) else None
            covers = info.get('imageLinks')
            cover = covers.get('thumbnail') if isinstance(covers, dict) else None
            if isinstance(cover, str) and cover.startswith('http://books.google.com/'):
                cover = 'https://' + cover.removeprefix('http://')
            if not isinstance(cover, str) or not cover.startswith('https://books.google.com/'):
                cover = None
            books.append(Book(source=self.name, source_id=source_id, title=title.strip(),
                authors=_str_list(info.get('authors')), language=language_tag, year=year,
                cover_url=cover, info_url='https://books.google.com/books?id=' + quote(source_id, safe=''),
                isbn13=isbn13(info.get('industryIdentifiers')),
                description=info.get('description', '')[:6000] if isinstance(info.get('description'), str) else None))
        return books


class Gutendex(Provider):
    name = "gutendex"
    purpose = "free_catalog"
    min_interval = 0.5

    async def search(self, client: httpx.AsyncClient, query: str, language: str | None, limit: int) -> list[Book]:
        params: dict[str, Any] = {"search": query}
        if language:
            params["languages"] = language
        data = await self.get_json(client, "https://gutendex.com/books/", params)
        books: list[Book] = []
        for item in data.get("results", [])[:limit]:
            if not isinstance(item, dict) or not isinstance(item.get("id"), int) or not isinstance(item.get("title"), str):
                continue
            raw_formats = item.get("formats") or {}
            formats = raw_formats if isinstance(raw_formats, dict) else {}
            epub = formats.get("application/epub+zip")
            # Gutenberg marks copyright status for the US; users must check local law.
            authorized = item.get("copyright") is False
            if not (isinstance(epub, str) and epub.startswith("https://") and authorized):
                epub = None
            authors = [a["name"] for a in item.get("authors", []) if isinstance(a, dict) and isinstance(a.get("name"), str)]
            cover = formats.get("image/jpeg")
            books.append(Book(
                source=self.name, source_id=str(item["id"]), title=item["title"], authors=authors,
                language=_language(item.get("languages")), year=None,
                cover_url=cover if isinstance(cover, str) and cover.startswith("https://") else None,
                info_url=f"https://www.gutenberg.org/ebooks/{item['id']}",
                epub_url=epub,
                availability="public_domain_us_check_local_law" if epub else "metadata_only",
            ))
        return books


class OpenLibrary(Provider):
    name = "openlibrary"
    purpose = "metadata"

    async def search(self, client: httpx.AsyncClient, query: str, language: str | None, limit: int, *, mode='all',page=1) -> list[Book]:
        params: dict[str, Any] = {"q": query, "limit": limit, "fields": "key,title,author_name,first_publish_year,language,cover_i,isbn"}
        if mode in {'author','title','isbn'}:
            params.pop('q');params[mode]=query
        if page>1:params['offset']=(page-1)*25
        if language:
            params["lang"] = language  # Ranking preference, not a strict filter.
        data = await self.get_json(client, "https://openlibrary.org/search.json", params)
        books: list[Book] = []
        for item in data.get("docs", [])[:limit]:
            if not isinstance(item, dict) or not isinstance(item.get("title"), str):
                continue
            key = item.get("key", "")
            if not isinstance(key, str) or not key.startswith("/works/") or not key.removeprefix("/works/").isalnum():
                continue
            # An Open Library work can represent editions in MANY languages.
            # "lang" is a ranking hint; never label a multilingual work as
            # German merely because some German edition exists.
            available = {_language([code]) for code in _str_list(item.get("language"))}
            available.discard(None)
            if language and available and language not in available:
                continue
            lang = next(iter(available)) if len(available) == 1 else None
            cover_id = item.get("cover_i")
            books.append(Book(
                source=self.name, source_id=key.removeprefix("/works/"),
                title=item["title"], authors=_str_list(item.get("author_name")), language=lang,
                year=item.get("first_publish_year") if isinstance(item.get("first_publish_year"), int) else None,
                cover_url=f"https://covers.openlibrary.org/b/id/{cover_id}-L.jpg" if isinstance(cover_id, int) else None,
                # Work-level ISBN lists mix different editions: never treat them as one exact edition.
                info_url=f"https://openlibrary.org{key}", isbn13=None,
                record_kind='work',
            ))
        return books

    async def search_page(self,client,query,language,limit,page,mode):
        return await self.search(client,query,language,min(limit,25),mode=mode,page=page)
