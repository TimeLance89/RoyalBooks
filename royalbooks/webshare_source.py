"""Anonymous Webshare search with full EPUB verification before publication.

Signed file links live only inside the request. A bounded private SQLite cache
holds the exact verified bytes so queueing never depends on an expired link.
"""
from __future__ import annotations

import asyncio
import hashlib
import io
import json
import re
import time
import zipfile
from pathlib import Path, PurePosixPath
from urllib.parse import urljoin, urlsplit

import httpx
from defusedxml import ElementTree as XML

from royalbooks.database import connect
from royalbooks.direct_sources import _record_id, index_epub
from royalbooks.matching import normalize
from royalbooks.providers import Book, Provider, ProviderError

MAX_FILE = 8 * 1024 * 1024
MAX_CACHE = 64 * 1024 * 1024
MAX_RECORDS = 32
CACHE_AGE = 86400
API = 'https://webshare.cz/api/'
IDENT = re.compile(r'[A-Za-z0-9]{6,20}')
SCHEMA = '''CREATE TABLE IF NOT EXISTS webshare_verified (
    record_id INTEGER PRIMARY KEY, ident TEXT UNIQUE NOT NULL,
    metadata TEXT NOT NULL, sha256 TEXT NOT NULL, epub BLOB NOT NULL,
    checked_at INTEGER NOT NULL)'''


def safe_page(url: str) -> bool:
    try:
        p = urlsplit(url)
        return (p.scheme == 'https' and p.hostname == 'webshare.cz'
                and not p.username and not p.password and p.port in (None, 443)
                and p.path == '/' and not p.query
                and re.fullmatch(r'/file/[A-Za-z0-9]{6,20}', p.fragment) is not None)
    except (ValueError, TypeError):
        return False


def safe_file(url: str) -> bool:
    try:
        p = urlsplit(url)
        return (p.scheme == 'https' and not p.username and not p.password
                and p.port in (None, 443) and not p.fragment
                and re.fullmatch(r'(?:free\.)?[0-9]+\.dl\.wsfiles\.cz', p.hostname or '') is not None)
    except (ValueError, TypeError):
        return False


def relevant(query: str, text: str) -> bool:
    # The source search is OR/fuzzy: never treat its response as a title match.
    stop = {'epub', 'deutsch', 'german', 'die', 'der', 'das', 'und', 'ein', 'eine', 'the', 'of', 'and'}
    wanted = set(normalize(query).split()) - stop
    return bool(wanted) and wanted <= set(normalize(text).split())


def inspect_epub(payload: bytes) -> tuple[bytes, dict, str]:
    """Repair only mimetype ordering/compression; preserve every content entry.

    Check archive bounds BEFORE decompression. The existing strict validator
    checks the resulting package again. No extraction or decryption occurs.
    """
    from royalbooks.jobs import AcquisitionError, MAX_UNCOMPRESSED, validate_epub
    import tempfile

    if not payload or len(payload) > MAX_FILE:
        raise ProviderError('webshare_file_size')
    try:
        with zipfile.ZipFile(io.BytesIO(payload)) as original:
            infos = original.infolist()
            names = [entry.filename for entry in infos]
            if not infos or len(infos) > 2500 or len(set(names)) != len(names):
                raise ProviderError('webshare_invalid_archive')
            total = 0
            for entry in infos:
                parts = PurePosixPath(entry.filename).parts
                if (not parts or entry.filename.startswith('/') or '..' in parts
                        or '\\' in entry.filename or entry.flag_bits & 1
                        or (entry.external_attr >> 16) & 0o170000 == 0o120000):
                    raise ProviderError('webshare_unsafe_archive')
                total += entry.file_size
                if total > MAX_UNCOMPRESSED or entry.file_size > max(1, entry.compress_size) * 250:
                    raise ProviderError('webshare_archive_limit')
            mime = original.getinfo('mimetype')
            if mime.file_size != 20 or original.read(mime) != b'application/epub+zip':
                raise ProviderError('webshare_invalid_mimetype')
            if infos[0].filename != 'mimetype' or mime.compress_type != zipfile.ZIP_STORED:
                fixed = io.BytesIO()
                with zipfile.ZipFile(fixed, 'w') as archive:
                    # Preserve a stable timestamp: repeated repairs must have
                    # identical hashes for duplicate detection after restarts.
                    archive.writestr(zipfile.ZipInfo('mimetype', mime.date_time),
                                     b'application/epub+zip', compress_type=zipfile.ZIP_STORED)
                    for entry in infos:
                        if entry.filename != 'mimetype':
                            archive.writestr(entry, original.read(entry))
                payload = fixed.getvalue()
                if len(payload) > MAX_FILE:
                    raise ProviderError('webshare_file_size')
        with tempfile.TemporaryDirectory(prefix='royalbooks-webshare-') as temp:
            path = Path(temp) / 'verified.epub'
            path.write_bytes(payload)
            digest = validate_epub(path)
        with zipfile.ZipFile(io.BytesIO(payload)) as archive:
            container = XML.fromstring(archive.read('META-INF/container.xml'))
            opf = next(node.attrib['full-path'] for node in container.findall('.//{*}rootfile')
                       if node.attrib.get('media-type') == 'application/oebps-package+xml')
            if archive.getinfo(opf).file_size > 1024 * 1024:
                raise ProviderError('webshare_metadata_size')
            package = XML.fromstring(archive.read(opf))
            metadata = package.find('{*}metadata')
            if metadata is None:
                raise ProviderError('webshare_metadata_missing')
            dc = '{http://purl.org/dc/elements/1.1/}'
            title = (metadata.findtext(dc + 'title') or '').strip()[:500]
            authors = list(dict.fromkeys((n.text or '').strip()[:200]
                                       for n in metadata.findall(dc + 'creator') if (n.text or '').strip()))[:32]
            langs = {(n.text or '').strip().lower().replace('_', '-').split('-')[0]
                     for n in metadata.findall(dc + 'language') if (n.text or '').strip()}
            langs = {{'ger': 'de', 'deu': 'de', 'eng': 'en'}.get(lang, lang) for lang in langs}
            if not title or len(langs) != 1:
                raise ProviderError('webshare_metadata_ambiguous')
            info = dict(title=title, authors=authors, language=next(iter(langs)),
                        description=(metadata.findtext(dc + 'description') or '')[:6000] or None)
        return payload, info, digest
    except (zipfile.BadZipFile, KeyError, StopIteration, XML.ParseError, AcquisitionError, RuntimeError, OSError) as exc:
        raise ProviderError('webshare_invalid_epub') from exc


def cached(db_path: str, record_id: int) -> tuple[bytes, dict] | None:
    """Read only; constructors and cache misses never create a database."""
    if not Path(db_path).is_file():
        return None
    import sqlite3
    try:
        with connect(db_path, timeout=5) as db:
            row = db.execute('SELECT epub,metadata,sha256,checked_at FROM webshare_verified WHERE record_id=?',
                             (record_id,)).fetchone()
    except sqlite3.OperationalError:
        return None
    if not row or time.time() - row[3] > CACHE_AGE or hashlib.sha256(row[0]).hexdigest() != row[2]:
        return None
    return row[0], json.loads(row[1])


def remember(db_path: str, ident: str, payload: bytes, metadata: dict, digest: str) -> int:
    if not IDENT.fullmatch(ident) or len(payload) > MAX_FILE or hashlib.sha256(payload).hexdigest() != digest:
        raise ProviderError('webshare_invalid_cache_record')
    Path(db_path).parent.mkdir(parents=True, exist_ok=True)
    record = _record_id('webshare', ident)
    with connect(db_path, timeout=5) as db:
        db.execute(SCHEMA)
        db.execute('DELETE FROM webshare_verified WHERE checked_at<?', (int(time.time()) - CACHE_AGE,))
        db.execute('INSERT OR REPLACE INTO webshare_verified VALUES(?,?,?,?,?,?)',
                   (record, ident, json.dumps(metadata), digest, payload, int(time.time())))
        # Eviction and insertion share a transaction, also under concurrent searches.
        while True:
            count, size = db.execute('SELECT COUNT(*),COALESCE(SUM(length(epub)),0) FROM webshare_verified').fetchone()
            if count <= MAX_RECORDS and size <= MAX_CACHE:
                break
            db.execute('DELETE FROM webshare_verified WHERE record_id=(SELECT record_id FROM webshare_verified ORDER BY checked_at,record_id LIMIT 1)')
    return index_epub(db_path, 'webshare', ident, f'https://webshare.cz/#/file/{ident}')


async def api(client: httpx.AsyncClient, method: str, data: dict):
    async with client.stream('POST', API + method + '/', data=data, follow_redirects=False,
                             headers={'Accept': 'text/xml; charset=UTF-8', 'X-Requested-With': 'XMLHttpRequest'},
                             timeout=3) as response:
        if response.status_code != 200:
            raise ProviderError(f'webshare_http_{response.status_code}')
        body = bytearray()
        async for block in response.aiter_bytes(65536):
            body.extend(block)
            if len(body) > 1024 * 1024:
                raise ProviderError('webshare_response_size')
    try:
        root = XML.fromstring(body)
    except (XML.ParseError, ValueError) as exc:
        raise ProviderError('webshare_invalid_response') from exc
    if root.findtext('status') != 'OK':
        raise ProviderError('webshare_source_unavailable')
    return root


async def fetch(client: httpx.AsyncClient, ident: str) -> bytes:
    root = await api(client, 'file_link', {'ident': ident, 'download_type': 'file_download', 'force_https': 1})
    url = root.findtext('link') or ''
    for _ in range(4):
        if not safe_file(url):
            raise ProviderError('webshare_untrusted_file')
        async with client.stream('GET', url, follow_redirects=False, timeout=4) as response:
            if response.is_redirect:
                url = urljoin(url, response.headers.get('location', ''))
                continue
            if response.status_code != 200:
                raise ProviderError(f'webshare_file_http_{response.status_code}')
            body = bytearray()
            async for block in response.aiter_bytes(65536):
                body.extend(block)
                if len(body) > MAX_FILE:
                    raise ProviderError('webshare_file_size')
            return bytes(body)
    raise ProviderError('webshare_redirect_limit')


class Webshare(Provider):
    name = 'webshare'
    purpose = 'verified_epub'

    def __init__(self, db_path: str):
        super().__init__()
        self.db_path = db_path
        self._search_cache = {}
        self._slots = asyncio.Semaphore(2)

    async def verified(self, client, ident):
        hit = await asyncio.to_thread(cached, self.db_path, _record_id(self.name, ident))
        if hit:
            return hit[1]
        async with self._slots:
            async with asyncio.timeout(4.5):
                payload = await fetch(client, ident)
                payload, metadata, digest = await asyncio.to_thread(inspect_epub, payload)
                await asyncio.to_thread(remember, self.db_path, ident, payload, metadata, digest)
                return metadata

    async def search(self, client, query, language, limit):
        key = normalize(query)
        async with self._lock:
            hit = self._search_cache.get(key)
            if hit and time.monotonic() - hit[0] < 300:
                candidates = hit[1]
            else:
                delay = max(0, self.min_interval - (time.monotonic() - self._last_request))
                if delay:
                    await asyncio.sleep(delay)
                self._last_request = time.monotonic()
                root = await api(client, 'search', {'what': query, 'limit': 100, 'offset': 0})
                candidates = []
                for row in root.findall('.//file')[:100]:
                    ident, name = row.findtext('ident') or '', row.findtext('name') or ''
                    try:
                        size = int(row.findtext('size') or '0')
                    except ValueError:
                        continue
                    if (IDENT.fullmatch(ident) and name.lower().endswith('.epub')
                            and 0 < size <= MAX_FILE and row.findtext('password') in (None, '', '0')
                            and relevant(query, name) and ident not in candidates):
                        candidates.append(ident)
                if len(self._search_cache) >= 64:
                    self._search_cache.pop(next(iter(self._search_cache)))
                self._search_cache[key] = (time.monotonic(), candidates)
        # Bound first-search work. Known wrong-language files do not occupy slots.
        selected, new_files = [], 0
        for ident in candidates:
            hit = await asyncio.to_thread(cached, self.db_path, _record_id(self.name, ident))
            if hit and language and hit[1]['language'] != language:
                continue
            if not hit:
                if new_files >= 2:
                    continue
                new_files += 1
            selected.append(ident)
            if len(selected) >= limit:
                break
        results = await asyncio.gather(*(self.verified(client, ident) for ident in selected), return_exceptions=True)
        books = []
        for ident, info in zip(selected, results):
            if isinstance(info, BaseException) or (language and info['language'] != language):
                continue
            if not relevant(query, info['title'] + ' ' + ' '.join(info['authors'])):
                continue
            record = _record_id(self.name, ident)
            books.append(Book(source=self.name, source_id=str(record), title=info['title'], authors=info['authors'],
                              language=info['language'], year=None, cover_url=None,
                              info_url=f'https://webshare.cz/#/file/{ident}', formats=('EPUB',),
                              description=info['description'], acquisition_id=record,
                              availability='verified_epub_cached', language_evidence='epub_metadata_verified'))
        return books
