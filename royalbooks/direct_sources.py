"""Publisher-listed, direct EPUB sources. No guessed file URLs or bypassed access.

Wikisource: completed-book metadata provided by the public WSIndex project.
Standard Ebooks: public 15-title *new releases* Atom feed (not members-only catalog).
The source's own listed EPUB link must be present and strictly allowlisted.
"""
from __future__ import annotations

import asyncio
import hashlib
import re
import sqlite3
import time
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

import httpx
from defusedxml import ElementTree

from royalbooks.providers import Book, Provider, ProviderError
from royalbooks.matching import normalize

SOURCES = frozenset({"wikisource", "standardebooks"})
ATOM = "{http://www.w3.org/2005/Atom}"
MAX_XML = 2 * 1024 * 1024
MAX_AGE = 7 * 24 * 3600

SCHEMA = """CREATE TABLE IF NOT EXISTS direct_source_catalog (
    source TEXT NOT NULL, source_id INTEGER NOT NULL, original_id TEXT NOT NULL,
    epub_url TEXT NOT NULL, discovered_at INTEGER NOT NULL,
    PRIMARY KEY (source, source_id)
)"""


def safe_epub_url(source: str, url: str) -> bool:
    if source not in SOURCES or not isinstance(url, str) or len(url) > 1200:
        return False
    try:
        p = urlsplit(url)
        if p.scheme != "https" or p.username or p.password or p.port not in (None, 443) or p.fragment:
            return False
        if source == "wikisource":
            if p.hostname not in {"ws-export-app.wmcloud.org", "ws-export.wmcloud.org"} or p.path not in ("", "/"):
                return False
            args = parse_qs(p.query, keep_blank_values=True)
            return (set(args) == {"format", "lang", "page"} and
                    args["format"] == ["epub-3"] and args["lang"] in (["de"], ["en"]) and
                    len(args["page"]) == 1 and 1 <= len(args["page"][0]) <= 200)
        if source == "standardebooks":
            return (p.hostname in {"standardebooks.org", "www.standardebooks.org",
                                   "downloads.standardebooks.org"}
                    and p.path.startswith("/ebooks/") and "/downloads/" in p.path
                    and p.path.lower().endswith(".epub") and not p.query)
    except ValueError:
        return False
    return False


def _record_id(source: str, original_id: str) -> int:
    raw = hashlib.sha256(f"{source}:{original_id}".encode("utf-8")).digest()
    return int.from_bytes(raw[:8], "big") & (2**53 - 1) or 1  # JS Number exact range


def index_epub(db_path: str, source: str, original_id: str, url: str) -> int:
    """Only called with provider-fetched metadata. Never accept a user URL via API."""
    if not original_id or len(original_id) > 300 or not safe_epub_url(source, url):
        raise ValueError("untrusted_direct_source_metadata")
    record_id = _record_id(source, original_id)
    Path(db_path).parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(db_path, timeout=10) as db:
        db.execute(SCHEMA)
        existing = db.execute(
            "SELECT original_id FROM direct_source_catalog WHERE source=? AND source_id=?",
            (source, record_id)).fetchone()
        if existing and existing[0] != original_id:
            raise ValueError("direct_source_id_collision")
        db.execute("""INSERT INTO direct_source_catalog (source,source_id,original_id,epub_url,discovered_at)
                      VALUES (?,?,?,?,?) ON CONFLICT(source,source_id) DO UPDATE SET
                      epub_url=excluded.epub_url,discovered_at=excluded.discovered_at""",
                   (source, record_id, original_id, url, int(time.time())))
    return record_id


def lookup_epub(db_path: str, source: str, record_id: int) -> str | None:
    if source not in SOURCES or type(record_id) is not int or record_id <= 0:
        return None
    try:
        with sqlite3.connect(db_path, timeout=10) as db:
            row = db.execute("""SELECT epub_url, discovered_at FROM direct_source_catalog
                                WHERE source=? AND source_id=?""",
                             (source, record_id)).fetchone()
    except sqlite3.OperationalError:
        return None
    if not row or int(time.time()) - row[1] > MAX_AGE or not safe_epub_url(source, row[0]):
        return None
    return row[0]


def _text(root, tag):
    elem = root.find(tag)
    return (elem.text or "").strip() if elem is not None else ""


def parse_standard_feed(payload: bytes, query: str, limit: int) -> list[tuple[str, str, list[str], str]]:
    """Parse *explicit* EPUB enclosure links; no speculative publisher paths."""
    if len(payload) > MAX_XML:
        raise ProviderError("feed_too_large")
    try:
        root = ElementTree.fromstring(payload)
    except (ElementTree.ParseError, ValueError) as exc:
        raise ProviderError("feed_invalid_xml") from exc
    results = []
    needle = normalize(query)
    for entry in root.findall(f"{ATOM}entry"):
        title = _text(entry, f"{ATOM}title")
        if not title:
            continue
        authors = [_text(author, f"{ATOM}name") for author in entry.findall(f"{ATOM}author")]
        authors = [a for a in authors if a]
        if needle not in normalize(title + " " + " ".join(authors)):
            continue
        eid = _text(entry, f"{ATOM}id")
        if not eid or len(eid) > 300:
            continue
        links = [el.attrib.get("href", "") for el in entry.findall(f"{ATOM}link")
                 if el.attrib.get("rel") == "enclosure"
                 and el.attrib.get("type") == "application/epub+zip"]
        url = next((link for link in links if safe_epub_url("standardebooks", link)), None)
        if url:
            results.append((eid, title, authors, url))
        if len(results) >= limit:
            break
    return results


class StandardEbooks(Provider):
    name = "standardebooks"
    purpose = "publisher_epub"
    min_interval = 3

    def __init__(self, db_path: str):
        super().__init__()
        self.db_path = db_path

    async def search(self, client: httpx.AsyncClient, query: str, language: str | None, limit: int) -> list[Book]:
        if language not in (None, "en"):
            return []
        async with self._lock:
            response = await client.get("https://standardebooks.org/feeds/atom/new-releases",
                                        headers={"Accept": "application/atom+xml"}, follow_redirects=False)
            if response.status_code != 200:
                raise ProviderError(f"standardebooks_http_{response.status_code}")
            if len(response.content) > MAX_XML:
                raise ProviderError("feed_too_large")
            entries = parse_standard_feed(response.content, query, limit)
        output = []
        for external_id, title, authors, url in entries:
            record_id = await asyncio.to_thread(index_epub, self.db_path, self.name, external_id, url)
            output.append(Book(source=self.name, source_id=str(record_id), title=title,
                               authors=authors, language="en", year=None, cover_url=None,
                               info_url="https://standardebooks.org/ebooks",
                               epub_url=url, availability="publisher_listed_epub_check_local_rights"))
        return output


class Wikisource(Provider):
    name = "wikisource"
    purpose = "publisher_epub"
    min_interval = 2

    def __init__(self, db_path: str):
        super().__init__()
        self.db_path = db_path

    async def search(self, client: httpx.AsyncClient, query: str, language: str | None, limit: int) -> list[Book]:
        # WSIndex lists completed works; its API is browsed one bounded page at a time.
        # This is not a global fulltext search. The result subset is filtered locally.
        lang = language
        params = {"page_size": min(100, max(limit * 6, 30))}
        if lang in ("de", "en"):
            params["languages"] = lang
        data = await self.get_json(client, "https://wsindex.toolforge.org/books/", params)
        result = []
        for row in data.get("results", [])[:100]:
            if not isinstance(row, dict):
                continue
            title, external_id, url = row.get("title"), row.get("wikidata_qid"), row.get("epub_url")
            if not isinstance(title, str) or not isinstance(external_id, str) or not re.fullmatch(r"Q[1-9]\d{0,15}", external_id):
                continue
            authors = []
            for entry in row.get("authors", []) if isinstance(row.get("authors"), list) else []:
                if isinstance(entry, str):
                    authors.append(entry)
                elif isinstance(entry, dict) and isinstance(entry.get("name"), str):
                    authors.append(entry["name"])
            if normalize(query) not in normalize(title + " " + " ".join(authors)):
                continue
            if not isinstance(url, str) or not safe_epub_url(self.name, url):
                continue
            ws_url = row.get("ws_url")
            if not isinstance(ws_url, str) or not (
                ws_url.startswith("https://de.wikisource.org/wiki/") or
                ws_url.startswith("https://en.wikisource.org/wiki/")):
                ws_url = "https://de.wikisource.org/"
            lang_value = parse_qs(urlsplit(url).query).get("lang", [None])[0]
            if lang in ("de", "en") and lang_value != lang:
                continue
            record_id = await asyncio.to_thread(index_epub, self.db_path, self.name, external_id, url)
            result.append(Book(source=self.name, source_id=str(record_id), title=title,
                               authors=authors[:5], language=lang_value, year=None, cover_url=None,
                               info_url=ws_url, epub_url=url,
                               availability="publisher_export_epub_check_local_rights"))
            if len(result) >= limit:
                break
        return result
