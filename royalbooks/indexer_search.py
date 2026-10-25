"""Read-only EBook indexer discovery for operator-configured Prowlarr/Newznab/Torznab.

Indexes may advertise titles and formats, but listings do not verify a file.
No grab/download URL, magnet, guid or secret is returned to the browser.
"""
from __future__ import annotations

import asyncio
import ipaddress
import json
import os
import re
from dataclasses import asdict, dataclass
from pathlib import Path
from urllib.parse import urlsplit

import httpx
from defusedxml import ElementTree as XML

BOOK_CATEGORY = 7020
MAX_RESPONSE_BYTES = 2 * 1024 * 1024
NN = "{http://www.newznab.com/DTD/2010/feeds/attributes/}"
FORMATS = ("epub", "azw3", "mobi", "pdf", "fb2", "djvu", "cbz")
LANGUAGE_DE = re.compile(r"(?i)(?:^|[^a-z])(?:german|deutsch|ger|deu)(?:[^a-z]|$)")
LANGUAGE_EN = re.compile(r"(?i)(?:^|[^a-z])(?:english|englisch|eng)(?:[^a-z]|$)")


@dataclass(frozen=True)
class Indexer:
    name: str
    kind: str
    url: str
    key: str


@dataclass(frozen=True)
class Listing:
    title: str
    indexer: str
    protocol: str
    language_hint: str | None
    format_hint: str | None
    size_bytes: int | None
    score: int

    def as_dict(self) -> dict:
        return asdict(self)


def validate_url(url: str, kind: str) -> str:
    """Only trust URLs supplied through server config, never a browser query."""
    if not isinstance(url, str) or not 8 <= len(url) <= 250:
        raise ValueError("invalid_indexer_url")
    try:
        p = urlsplit(url)
        if p.scheme not in {"https", "http"} or not p.hostname or p.username or p.password:
            raise ValueError("invalid_indexer_url")
        if p.query or p.fragment or (kind == "prowlarr" and p.path not in ("", "/")):
            raise ValueError("invalid_indexer_url")
        if p.path and any(x in p.path.lower() for x in ("..", "%2f", "%5c")):
            raise ValueError("invalid_indexer_url")
        _ = p.port
        if p.scheme == "http":
            try:
                address = ipaddress.ip_address(p.hostname)
                allowed = address.is_private or address.is_loopback
            except ValueError:
                # Single-label container hostname, not an internet domain.
                allowed = bool(re.fullmatch(r"[A-Za-z0-9-]{1,63}", p.hostname))
            if not allowed:
                raise ValueError("public_http_indexer_refused")
    except ValueError as exc:
        raise ValueError("invalid_indexer_url") from exc
    return url.rstrip("/")


def configured_indexers() -> list[Indexer]:
    result = []
    host = os.getenv("ROYALBOOKS_PROWLARR_URL", "").strip()
    key = os.getenv("ROYALBOOKS_PROWLARR_API_KEY", "").strip()
    if host and key:
        result.append(Indexer("Prowlarr", "prowlarr", validate_url(host, "prowlarr"), key))
    path = Path(os.getenv("ROYALBOOKS_INDEXERS_PATH", "/data/indexers.json"))
    if path.is_file() and not path.is_symlink():
        if path.stat().st_size > 16384:
            raise ValueError("indexer_config_too_large")
        records = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(records, list) or len(records) > 8:
            raise ValueError("invalid_indexer_config")
        for entry in records:
            if not isinstance(entry, dict):
                raise ValueError("invalid_indexer_config")
            name, kind, url, secret = [entry.get(k) for k in ("name", "kind", "url", "api_key")]
            if not isinstance(name, str) or not re.fullmatch(r"[A-Za-z0-9 _-]{2,40}", name):
                raise ValueError("invalid_indexer_name")
            if kind not in {"torznab", "newznab"} or not isinstance(secret, str) or not secret:
                raise ValueError("invalid_indexer_config")
            result.append(Indexer(name, kind, validate_url(url, kind), secret))
    return result


def infer_language(title: str) -> str | None:
    de, en = bool(LANGUAGE_DE.search(title)), bool(LANGUAGE_EN.search(title))
    return "de" if de and not en else "en" if en and not de else None


def infer_format(title: str) -> str | None:
    for extension in FORMATS:
        if re.search(r"(?i)(?:^|[^a-z0-9])" + extension + r"(?:[^a-z0-9]|$)", title):
            return extension.upper()
    return None


def make_listing(title: object, provider: str, protocol: str, size: object,
                 query: str, language: str | None) -> Listing | None:
    if not isinstance(title, str) or not 2 <= len(title) <= 250:
        return None
    lang = infer_language(title)
    if language and lang and language != lang:
        return None
    fmt = infer_format(title)
    terms = set(word for word in re.findall(r"\w+", query.casefold()) if len(word) > 2)
    title_terms = set(re.findall(r"\w+", title.casefold()))
    relevance = round(65 * len(terms & title_terms) / len(terms)) if terms else 0
    relevance += {"EPUB": 15, "AZW3": 10, "PDF": 5}.get(fmt, 0)
    if language and lang == language:
        relevance += 20
    return Listing(
        title=title, indexer=provider[:60], protocol=protocol,
        language_hint=lang, format_hint=fmt,
        size_bytes=size if type(size) is int and 0 < size < 2 * 1024**4 else None,
        score=min(100, relevance),
    )


def parse_prowlarr(data: object, query: str, language: str | None, limit: int) -> list[Listing]:
    if not isinstance(data, list):
        raise ValueError("invalid_prowlarr_shape")
    found = []
    for item in data[:200]:
        if not isinstance(item, dict):
            continue
        categories = item.get("categories", [])
        ids = [c if type(c) is int else c.get("id") if isinstance(c, dict) else None
               for c in categories] if isinstance(categories, list) else []
        if BOOK_CATEGORY not in ids:
            continue
        name = item.get("indexer")
        proto = item.get("protocol")
        listed = make_listing(
            item.get("title"), name if isinstance(name, str) else "Prowlarr",
            proto if proto in ("torrent", "usenet") else "unknown",
            item.get("size"), query, language,
        )
        if listed:
            found.append(listed)
    return sorted(found, key=lambda r: -r.score)[:limit]


def parse_rss(payload: bytes, config: Indexer, query: str,
              language: str | None, limit: int) -> list[Listing]:
    if len(payload) > MAX_RESPONSE_BYTES:
        raise ValueError("indexer_feed_too_large")
    root = XML.fromstring(payload)
    if root.tag != "rss":
        raise ValueError("indexer_not_rss")
    output = []
    for node in root.findall("./channel/item")[:200]:
        attrs = {el.attrib.get("name"): el.attrib.get("value")
                 for el in node.findall(NN + "attr")}
        category = attrs.get("category")
        if category not in ("7020", "7000"):
            continue
        size = attrs.get("size") or node.findtext("size")
        listed = make_listing(node.findtext("title"), config.name, config.kind,
                              int(size) if size and size.isdecimal() else None,
                              query, language)
        if listed:
            output.append(listed)
    return sorted(output, key=lambda r: -r.score)[:limit]


async def fetch_indexer(client: httpx.AsyncClient, config: Indexer, query: str,
                        language: str | None, limit: int) -> list[Listing]:
    if config.kind == "prowlarr":
        path = config.url + "/api/v1/search"
        params = {"query": query, "type": "search", "categories": BOOK_CATEGORY}
        headers = {"X-Api-Key": config.key}
    else:
        path = config.url
        params = {"t": "search", "q": query, "cat": BOOK_CATEGORY, "apikey": config.key}
        headers = {"Accept": "application/rss+xml"}
    response = await client.get(path, params=params, headers=headers, follow_redirects=False)
    if response.status_code != 200:
        raise ValueError("indexer_http_" + str(response.status_code))
    if len(response.content) > MAX_RESPONSE_BYTES:
        raise ValueError("indexer_response_too_large")
    if config.kind == "prowlarr":
        return parse_prowlarr(response.json(), query, language, limit)
    return parse_rss(response.content, config, query, language, limit)


async def search_indexers(query: str, language: str | None, limit: int = 30) -> dict:
    sources = configured_indexers()
    if not sources:
        return {"configured": False, "sources": [], "items": []}
    async with httpx.AsyncClient(timeout=httpx.Timeout(8.0), follow_redirects=False,
                                 trust_env=False) as client:
        async def check(src):
            try:
                items = await asyncio.wait_for(
                    fetch_indexer(client, src, query, language, limit), timeout=12)
                return items, {"name": src.name, "status": "ok", "count": len(items)}
            except (ValueError, httpx.HTTPError, asyncio.TimeoutError):
                return [], {"name": src.name, "status": "unavailable", "count": 0}
        results = await asyncio.gather(*(check(src) for src in sources))
    books = [item for group, _ in results for item in group]
    books.sort(key=lambda r: -r.score)
    return {"configured": True,
            "sources": [status for _, status in results],
            "items": [book.as_dict() for book in books[:limit]]}
