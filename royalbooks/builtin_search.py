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
from royalbooks.providers import Book, Provider, ProviderError
from royalbooks.direct_sources import index_epub

from royalbooks.vendor import lgsearch

MIRRORS = (
    "https://libgen.li/",
    "https://libgen.gl/",
    "https://libgen.bz/",
    "https://libgen.la/",
)
FORMATS = {"epub", "pdf", "azw3", "mobi", "fb2", "djvu", "txt"}
BOOK_TOPICS = "lf"
MAX_HTML_BYTES = 2 * 1024 * 1024
PER_PAGE = 100
MAX_PAGES = 3
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
    return [book for cells in parser.rows
            if (book := lgsearch.row_to_entry(cells)) is not None]


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
        ))
    # German before unknown, with EPUB and title relevance as tie breakers.
    books.sort(key=lambda x: (-x.score, x.format_hint != "EPUB", x.title.casefold()))
    return books[:limit]


async def _search_one(client: httpx.AsyncClient, mirror: str,
                      query: str, language: str | None, limit: int) -> list[DirectBook]:
    collected: list[dict] = []
    for page in range(1, MAX_PAGES + 1):
        url = lgsearch.build_url(mirror, query, BOOK_TOPICS, page, PER_PAGE, "ta")
        response = await client.get(url, follow_redirects=False)
        if response.status_code != 200:
            raise ValueError("catalogue_http_" + str(response.status_code))
        if len(response.content) > MAX_HTML_BYTES:
            raise ValueError("catalogue_page_too_large")
        items = parse_html(response.text)
        collected.extend(items)
        available = present(collected, query, language, mirror, limit)
        if len(available) >= limit or len(items) < PER_PAGE:
            return available
    return present(collected, query, language, mirror, limit)


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
                    books = []
                    for entry in entries:
                        acquisition_id = None
                        if self.db_path and entry.format_hint == 'EPUB':
                            acquisition_id = await asyncio.to_thread(index_epub, self.db_path, self.name,
                                entry.source_id, mirror + 'ads.php?md5=' + entry.source_id)
                        books.append(Book(source=self.name,
                            source_id=entry.source_id or hashlib.sha256((entry.source_url or entry.title).encode()).hexdigest()[:32],
                            title=entry.title, authors=[entry.author] if entry.author else [],
                            language=entry.language_hint,
                            year=int(entry.year) if re.fullmatch(r'\d{4}', entry.year) else None,
                            cover_url=None, info_url=entry.source_url or mirror,
                            availability=entry.availability,
                            formats=(entry.format_hint,) if entry.format_hint else (),
                            language_evidence='catalog_claim_unverified', acquisition_id=acquisition_id))
                    return books
                except ValueError as exc:
                    if str(exc) in {'catalogue_http_403', 'catalogue_http_451', 'catalogue_http_429', 'catalogue_table_missing'}:
                        self._blocked_until = monotonic() + 1800
                        raise ProviderError(str(exc)) from exc
                except httpx.HTTPError:
                    continue
            raise ProviderError('catalogue_unavailable')
