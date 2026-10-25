"""RoyalBooks built-in no-setup ebook discovery.

The default provider is a small *in-process* LibGen catalogue adapter:
no Prowlarr, Torznab/Newznab API key, browser extension or extra container.
Only trusted HTTPS mirrors are queried. Listings are metadata, not verified
downloads or proof that the rights holder authorizes a transfer.

HTML table parsing is based on the vendored MIT-licensed lgsearch parser.
"""
from __future__ import annotations

import asyncio
import re
import hashlib
from dataclasses import asdict, dataclass
from urllib.parse import urljoin

import httpx
from royalbooks.providers import Book, Provider, ProviderError, canonical_isbn
from royalbooks.direct_sources import index_epub

from royalbooks.vendor import lgsearch
from royalbooks.artwork import register_artwork

MIRRORS = (
    "https://libgen.li/",
    "https://libgen.gl/",
    "https://libgen.bz/",
    "https://libgen.la/",
)
FORMATS = {"epub", "pdf", "azw3", "mobi", "fb2", "djvu", "txt"}
BOOK_TOPICS = "lf"
MAX_HTML_BYTES = 2 * 1024 * 1024
PER_PAGE = 25  # The live catalog caps the rendered table at 25, even with res=100.
MAX_PAGES = 4
EDITION_PATH = re.compile(r"^edition\.php\?id=([0-9]{1,12})$")


@dataclass(frozen=True)
class DirectBook:
    title: str
    author: str
    indexer: str
    protocol: str
    language_hint: str | None
    format_hint: str | None
    size_bytes: int | None
    score: int
    source_url: str | None
    publisher: str
    year: str
    availability: str = "catalog_listing_unverified"
    source_id: str = ""
    cover_url: str | None = None
    isbns: tuple[str, ...] = ()

    def as_dict(self) -> dict:
        return asdict(self)


def _language(value: str) -> str | None:
    text = re.sub(r"\s+", " ", value.strip().casefold())
    if text in {"german", "deutsch", "de", "ger", "deu", "german; deutsch"}:
        return "de"
    if text in {"english", "englisch", "en", "eng"}:
        return "en"
    return None


def _size_bytes(value: str) -> int | None:
    match = re.fullmatch(r"\s*([\d.]+)\s*([kmg])?b\s*", value, re.IGNORECASE)
    if not match:
        return None
    try:
        number = float(match.group(1))
    except ValueError:
        return None
    scale = {"": 1, "k": 1024, "m": 1024**2, "g": 1024**3}
    result = int(number * scale[match.group(2).casefold() if match.group(2) else ""])
    return result if 0 < result < 2 * 1024**4 else None


def _source_url(mirror: str, href: object) -> str | None:
    if not isinstance(href, str):
        return None
    match = EDITION_PATH.fullmatch(href)
    return urljoin(mirror, "edition.php?id=" + match.group(1)) if match else None


def parse_html(page: str) -> list[dict]:
    """Fail explicitly on blocked/login pages, not as silent zero matches."""
    if lgsearch.is_overloaded(page):
        raise ValueError("catalogue_overloaded")
    if "tablelibgen" not in page:
        if any(word in page.casefold() for word in (
            "no records found", "nothing found", "no results found")):
            return []
        raise ValueError("catalogue_table_missing")
    parser = lgsearch.ResultTableParser()
    parser.feed(page)
    output = []
    for raw in parser.rows:
        has_cover = len(raw) == 10 and bool(raw[0].get('images'))
        cells = raw[1:] if has_cover else raw
        book = lgsearch.row_to_entry(cells)
        if book:
            book['cover_path'] = raw[0]['images'][0] if has_cover else None
            book['isbns'] = tuple(dict.fromkeys(number for link in cells[0]['links']
                if (number := canonical_isbn(link.get('text', '').replace('-', '')))))
            output.append(book)
    return output


def present(entries: list[dict], query: str, language: str | None,
            mirror: str, limit: int) -> list[DirectBook]:
    terms = {w for w in re.findall(r"\w+", query.casefold()) if len(w) >= 2}
    books: list[DirectBook] = []
    seen: set[str] = set()
    for item in entries:
        title = str(item.get("title", "")).strip()
        fmt = str(item.get("extension", "")).strip().lower()
        if not title or len(title) > 350 or fmt not in FORMATS:
            continue
        origin_language = str(item.get("language", ""))
        lang = _language(origin_language)
        if language and lang and lang != language:
            continue
        md5 = item.get("md5")
        # MD5 is a catalog identifier, never treated as a validated download.
        if not isinstance(md5, str) or not re.fullmatch(r"[0-9a-fA-F]{32}", md5):
            continue
        if md5 in seen:
            continue
        seen.add(md5)
        title_terms = set(re.findall(r"\w+", title.casefold()))
        overlap = 65 * len(title_terms & terms) // len(terms) if terms else 0
        relevance = overlap + {"epub": 15, "azw3": 9, "mobi": 6, "pdf": 4}.get(fmt, 0)
        if language:
            relevance += 20 if lang == language else 0
        else:
            relevance += 8 if lang == "de" else 0
        books.append(DirectBook(
            title=title, author=str(item.get("author", ""))[:200],
            indexer="Library Genesis (Direktquelle)", protocol="direct_catalog",
            language_hint=lang, format_hint=fmt.upper(),
            size_bytes=_size_bytes(str(item.get("size", ""))),
            score=max(0, min(100, relevance)),
            source_url=_source_url(mirror, item.get("href")),
            publisher=str(item.get("publisher", ""))[:100],
            year=str(item.get("year", ""))[:12],
            source_id=md5.lower(),
            cover_url=urljoin(mirror, item['cover_path']) if isinstance(item.get('cover_path'),str)
                and re.fullmatch(r'/(?:covers|fictioncovers)/[0-9]+/'+md5.lower()+r'(?:_small)?\.jpg',item['cover_path']) else None,
            isbns=item.get('isbns', ()),
        ))
    # German before unknown, with EPUB and title relevance as tie breakers.
    books.sort(key=lambda x: (-x.score, x.format_hint != "EPUB", x.title.casefold()))
    return books[:limit]


async def _search_one(client: httpx.AsyncClient, mirror: str,
                      query: str, language: str | None, limit: int, order: str | None = None, topics: str = BOOK_TOPICS,
                      field: str | None = None, start_page: int = 1, page_count: int = MAX_PAGES) -> list[DirectBook]:
    # The source documents lang/ext operators in its own Google-mode help.
    # Filter upstream so German EPUBs are not buried behind foreign PDFs.
    language_filter = {'de': ' lang:ger', 'en': ' lang:eng'}.get(language, '')
    fields = {'author':'a','title':'t','isbn':'i'}.get(field) or ('i' if canonical_isbn(query) else 'ta')
    for format_filter in (' ext:epub', ''):
        collected: list[dict] = []
        seen_pages = set()
        for page in range(start_page, start_page + page_count):
            url = lgsearch.build_url(mirror, query + language_filter + format_filter,
                                     topics, page, PER_PAGE, fields)
            url += '&covers=on'
            if order == 'recent':
                url += '&order=time_added&ordermode=desc'
            response = await client.get(url, follow_redirects=False)
            if response.status_code != 200:
                raise ValueError("catalogue_http_" + str(response.status_code))
            if len(response.content) > MAX_HTML_BYTES:
                raise ValueError("catalogue_page_too_large")
            items = parse_html(response.text)
            fingerprint = tuple(item.get('md5') for item in items)
            if fingerprint in seen_pages:
                break
            seen_pages.add(fingerprint)
            collected.extend(items)
            available = present(collected, query, language, mirror, limit)
            if len(available) >= limit or len(items) < PER_PAGE:
                break
        if collected:
            return present(collected, query, language, mirror, limit)
    return []


async def search_builtin(query: str, language: str | None, limit: int = 30) -> dict:
    """Visit pinned mirrors in order, stop on first valid catalogue response."""
    if not 2 <= len(query.strip()) <= 120 or language not in {None, "de", "en"}:
        raise ValueError("invalid_direct_search")
    limit = max(1, min(limit, 40))
    states: list[dict] = []
    async with httpx.AsyncClient(
        timeout=httpx.Timeout(4.0), follow_redirects=False, trust_env=False,
        headers={"Accept": "text/html", "User-Agent": "RoyalBooks/1.1 (book catalogue search)"},
    ) as client:
        for mirror in MIRRORS:
            try:
                found = await asyncio.wait_for(
                    _search_one(client, mirror, query, language, limit), timeout=9.0)
                states.append({
                    "name": "Library Genesis",
                    "status": "ok" if found else "empty",
                    "count": len(found),
                    "mirror": mirror.rstrip("/").split("//", 1)[1],
                })
                return {"configured": True, "items": [b.as_dict() for b in found],
                        "sources": states, "mode": "built_in_direct",
                        "notice": "Catalog results are unverified; no file has been downloaded."}
            except (httpx.HTTPError, asyncio.TimeoutError, ValueError, UnicodeError):
                states.append({
                    "name": "Library Genesis",
                    "status": "unavailable", "count": 0,
                    "mirror": mirror.rstrip("/").split("//", 1)[1],
                })
    return {"configured": True, "items": [], "sources": states,
            "mode": "built_in_direct",
            "notice": "Every built-in catalogue mirror was unavailable or incompatible."}


class LibraryGenesis(Provider):
    """Existing catalog parser behind the same contract as every other source.

    Selected EPUBs obtain source-bound acquisition IDs. No inferred entitlement.
    An explicit access refusal stops
    fallback, rather than trying another origin to evade a restriction.
    """
    name = 'libgen'
    purpose = 'file_listing'
    min_interval = 2

    def __init__(self, db_path: str | None = None):
        super().__init__()
        self.db_path = db_path

    async def search(self, client, query, language, limit):
        from time import monotonic
        async with self._lock:
            if self.cooldown_remaining():
                raise ProviderError('source_cooldown')
            delay = max(0, self.min_interval - (monotonic() - self._last_request))
            if delay:
                await asyncio.sleep(delay)
            for mirror in MIRRORS:
                self._last_request = monotonic()
                try:
                    try:
                        entries = await _search_one(client, mirror, query, language, limit)
                    except ValueError as exc:
                        if str(exc) != 'catalogue_overloaded':
                            raise
                        await asyncio.sleep(.5)
                        entries = await _search_one(client, mirror, query, language, limit)
                    return await self._books(entries, mirror)
                except ValueError as exc:
                    if str(exc) in {'catalogue_http_403', 'catalogue_http_451', 'catalogue_http_429', 'catalogue_table_missing'}:
                        self._blocked_until = monotonic() + 1800
                        raise ProviderError(str(exc)) from exc
                except httpx.HTTPError:
                    continue
            raise ProviderError('catalogue_unavailable')

    async def _books(self, entries, mirror):
        books = []
        for entry in entries:
            if self.db_path and entry.cover_url:
                await asyncio.to_thread(register_artwork,self.db_path,entry.source_id,entry.cover_url)
            acquisition_id = None
            if self.db_path and entry.format_hint == 'EPUB':
                acquisition_id = await asyncio.to_thread(index_epub, self.db_path, self.name,
                    entry.source_id, mirror + 'ads.php?md5=' + entry.source_id)
            books.append(Book(source=self.name,
                source_id=entry.source_id or hashlib.sha256((entry.source_url or entry.title).encode()).hexdigest()[:32],
                title=entry.title, authors=[entry.author] if entry.author else [],
                language=entry.language_hint,
                year=int(entry.year) if re.fullmatch(r'\d{4}', entry.year) else None,
                cover_url=entry.cover_url, isbns=entry.isbns,
                isbn13=entry.isbns[0] if entry.isbns else None, info_url=entry.source_url or mirror,
                availability=entry.availability,
                formats=(entry.format_hint,) if entry.format_hint else (),
                language_evidence='catalog_claim_unverified', acquisition_id=acquisition_id))
        return books

    async def search_page(self,client,query,language,limit,page,mode):
        from time import monotonic
        async with self._lock:
            if self.cooldown_remaining():raise ProviderError('source_cooldown')
            await asyncio.sleep(max(0,self.min_interval-(monotonic()-self._last_request)))
            for mirror in MIRRORS:
                self._last_request=monotonic()
                try:
                    entries=await _search_one(client,mirror,query,language,min(limit,25),
                                             field=mode,start_page=page,page_count=1)
                    return await self._books(entries,mirror)
                except ValueError as exc:
                    if str(exc) in {'catalogue_http_403','catalogue_http_451','catalogue_http_429','catalogue_table_missing'}:
                        self._blocked_until=monotonic()+1800;raise ProviderError(str(exc)) from exc
                except httpx.HTTPError:continue
            raise ProviderError('catalogue_unavailable')

    async def latest(self, client, language='de', limit=24, topics=BOOK_TOPICS, year=None):
        from time import monotonic
        async with self._lock:
            if self.cooldown_remaining():
                raise ProviderError('source_cooldown')
            await asyncio.sleep(max(0,self.min_interval-(monotonic()-self._last_request)))
            for mirror in MIRRORS:
                self._last_request=monotonic()
                try:
                    query='year:'+str(year) if type(year) is int else ''
                    entries=await _search_one(client,mirror,query,language,limit,order='recent',topics=topics)
                    return await self._books([entry for entry in entries if entry.format_hint == 'EPUB'],mirror)
                except ValueError as exc:
                    if str(exc) in {'catalogue_http_403','catalogue_http_451','catalogue_http_429','catalogue_table_missing'}:
                        self._blocked_until=monotonic()+1800
                        raise ProviderError(str(exc)) from exc
                except httpx.HTTPError:
                    continue
            raise ProviderError('catalogue_unavailable')
