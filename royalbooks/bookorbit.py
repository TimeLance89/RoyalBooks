"""BookOrbit's published HTTP API and crash-safe Book Dock handoff.

No PostgreSQL access. A handoff receipt is never an import acknowledgement.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import os
import re
import shutil
import stat
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlsplit

import httpx

from royalbooks.database import connect


class BookOrbitError(Exception):
    """Controlled codes only; credentials/remote response bodies are never logged."""


@dataclass(frozen=True)
class Config:
    url: str = ''
    public_url: str = ''
    ingest: str = ''
    library: str = ''
    secret_file: str = '/run/secrets/bookorbit.env'
    verify_import: bool = False

    @classmethod
    def environment(cls):
        return cls(url=os.getenv('ROYALBOOKS_BOOKORBIT_URL', '').rstrip('/'),
                   public_url=os.getenv('ROYALBOOKS_BOOKORBIT_PUBLIC_URL', '').rstrip('/'),
                   ingest=os.getenv('ROYALBOOKS_BOOKORBIT_INGEST_PATH', ''),
                   library=os.getenv('ROYALBOOKS_BOOKORBIT_LIBRARY_PATH', ''),
                   secret_file=os.getenv('ROYALBOOKS_BOOKORBIT_SECRET_FILE', '/run/secrets/bookorbit.env'),
                   verify_import=os.getenv('ROYALBOOKS_BOOKORBIT_VERIFY_IMPORT', '0') == '1')


def checked_url(value: str) -> str:
    try:
        p = urlsplit(value)
        if p.scheme not in {'http', 'https'} or not p.hostname or p.username or p.password or p.query or p.fragment or p.path not in {'', '/'}:
            raise ValueError()
        p.port
    except (TypeError, ValueError):
        raise BookOrbitError('bookorbit_url_invalid') from None
    return value.rstrip('/')


def credentials(filename: str) -> dict:
    """Read an operator-owned secret, never shell-source it or interpolate values."""
    path = Path(filename)
    try:
        if path.is_symlink():
            raise BookOrbitError('bookorbit_secret_unsafe')
        with path.open('rb') as stream:
            info = os.fstat(stream.fileno())
            if not stat.S_ISREG(info.st_mode) or info.st_size > 16384:
                raise BookOrbitError('bookorbit_secret_unsafe')
            if os.name != 'nt' and info.st_mode & 0o077:
                raise BookOrbitError('bookorbit_secret_permissions')
            content = stream.read(16385).decode('utf-8-sig')
    except FileNotFoundError:
        raise BookOrbitError('bookorbit_credentials_missing') from None
    except (OSError, UnicodeError):
        raise BookOrbitError('bookorbit_secret_unreadable') from None
    result = {}
    for line in content.splitlines():
        line = line.strip()
        if not line or line.startswith('#'):
            continue
        key, sep, value = line.partition('=')
        if not sep or key not in {'BOOKORBIT_USERNAME', 'BOOKORBIT_PASSWORD'} or key in result:
            raise BookOrbitError('bookorbit_secret_invalid')
        if len(value) >= 2 and value[0] == value[-1] and value[0] in {'"', "'"}:
            value = value[1:-1]
        result[key] = value
    if not result.get('BOOKORBIT_USERNAME') or not result.get('BOOKORBIT_PASSWORD'):
        raise BookOrbitError('bookorbit_credentials_missing')
    return result


class Client:
    def __init__(self, config: Config, *, transport=None):
        self.config = config
        self.http = httpx.AsyncClient(timeout=httpx.Timeout(8, connect=3), trust_env=False,
                                      follow_redirects=False, transport=transport,
                                      limits=httpx.Limits(max_connections=4))
        self._lock = asyncio.Lock()
        self._access = self._refresh = None
        self._expires = 0.0
        self._backoff = 0.0

    async def close(self):
        self._access = self._refresh = None
        await self.http.aclose()

    async def _request(self, method, path, *, headers=None, **kwargs):
        url = checked_url(self.config.url) + '/api/v1/' + path
        try:
            async with self.http.stream(method, url, headers=headers, **kwargs) as response:
                if response.status_code >= 300:
                    raise BookOrbitError('bookorbit_http_' + str(response.status_code))
                data = bytearray()
                async for block in response.aiter_bytes(65536):
                    data.extend(block)
                    if len(data) > 4 * 1024 * 1024:
                        raise BookOrbitError('bookorbit_response_too_large')
                payload = json.loads(data) if data else {}
                if not isinstance(payload, dict):
                    raise BookOrbitError('bookorbit_response_invalid')
                return payload
        except (httpx.RequestError, ValueError):
            raise BookOrbitError('bookorbit_unavailable') from None

    def _accept(self, payload):
        try:
            access, refresh = payload['accessToken'], payload['refreshToken']
            expiry = datetime.fromisoformat(payload['accessTokenExpiresAt'].replace('Z', '+00:00')).timestamp()
            if not isinstance(access, str) or not access or not re.fullmatch(r'[a-f0-9]{64}', refresh) or expiry <= time.time():
                raise ValueError()
        except (KeyError, TypeError, ValueError, AttributeError):
            raise BookOrbitError('bookorbit_session_invalid') from None
        self._access, self._refresh, self._expires = access, refresh, expiry
        # Native refresh uses the body; do not accidentally send a stale web cookie.
        self.http.cookies.clear()

    async def _authenticate(self, *, force=False, rejected=None):
        async with self._lock:
            if rejected and self._access and self._access != rejected:
                return
            if not force and self._access and self._expires > time.time() + 30:
                return
            if time.monotonic() < self._backoff:
                raise BookOrbitError('bookorbit_auth_cooldown')
            if self._refresh:
                try:
                    self._accept(await self._request('POST', 'auth/refresh', json={'refreshToken': self._refresh}))
                    return
                except BookOrbitError as exc:
                    if str(exc) not in {'bookorbit_http_401', 'bookorbit_http_403'}:
                        raise
                    self._access = self._refresh = None
            try:
                secret = await asyncio.to_thread(credentials, self.config.secret_file)
                self._accept(await self._request('POST', 'auth/login', json={
                    'username': secret['BOOKORBIT_USERNAME'], 'password': secret['BOOKORBIT_PASSWORD'],
                    'clientKind': 'native', 'deviceLabel': 'RoyalBooks'}))
            except BookOrbitError:
                self._backoff = time.monotonic() + 60
                raise

    async def request(self, method, path, **kwargs):
        await self._authenticate()
        access = self._access
        try:
            return await self._request(method, path, headers={'Authorization': 'Bearer ' + access}, **kwargs)
        except BookOrbitError as exc:
            if str(exc) != 'bookorbit_http_401':
                raise
        await self._authenticate(force=True, rejected=access)
        # One replay only. Callers use this for read operations, not finalize.
        return await self._request(method, path, headers={'Authorization': 'Bearer ' + self._access}, **kwargs)

    async def connection(self):
        result = {'backend': 'bookorbit', 'reachable': False, 'authenticated': False,
                  'public_url': self.config.public_url, 'verify_import': self.config.verify_import}
        try:
            health = await self._request('GET', 'health')
            result['reachable'] = health.get('status') == 'ok'
            await self.request('GET', 'book-dock/summary')
            result['authenticated'] = True
        except BookOrbitError as exc:
            result['error'] = str(exc)
        return result

    async def shelf(self, query='', sort='recent', limit=60, offset=0):
        # BookOrbit paginates by zero-based page, RoyalBooks by row offset.
        # Fetch aligned pages and slice so arbitrary offsets remain correct.
        field = {'recent': 'addedAt', 'title': 'title', 'author': 'author'}[sort]
        start, end = offset // limit, (offset + limit - 1) // limit
        pages = []
        for page in range(start, end + 1):
            data = await self.request('POST', 'books/query', json={
                'q': query, 'sort': [{'field': field, 'dir': 'desc' if sort == 'recent' else 'asc'}],
                'pagination': {'page': page, 'size': limit}})
            if not isinstance(data.get('items'), list) or type(data.get('total')) is not int:
                raise BookOrbitError('bookorbit_library_response_invalid')
            pages.extend(data['items'])
        selected = pages[offset % limit:offset % limit + limit]
        items = []
        for book in selected:
            ident = book.get('id')
            if type(ident) is not int or ident <= 0:
                raise BookOrbitError('bookorbit_library_response_invalid')
            formats = list(dict.fromkeys(str(f.get('format', '')).upper() for f in book.get('files', [])))
            items.append({'id': ident, 'title': book.get('title') or 'Ohne Titel',
                          'authors': ', '.join(book.get('authors') or []) or 'Autor unbekannt',
                          'description': book.get('description'), 'formats': formats,
                          'added_at': book.get('addedAt'), 'cover_url': f'/api/library/{ident}/cover',
                          'epub_url': f'/api/library/{ident}/epub' if 'EPUB' in formats else None})
        return {'items': items, 'total': data['total'], 'filtered_total': data['total'],
                'offset': offset, 'limit': limit, 'has_more': offset + len(items) < data['total']}

    async def asset(self, book_id, kind):
        if kind == 'cover':
            path = f'books/{book_id}/cover'
            maximum = 8 * 1024 * 1024
        else:
            detail = await self.request('GET', f'books/{book_id}')
            files = detail.get('files', [])
            item = next((f for f in files if str(f.get('format', '')).lower() == 'epub'), None)
            if not item or type(item.get('id')) is not int:
                raise BookOrbitError('bookorbit_http_404')
            path, maximum = f"books/files/{item['id']}/download", 32 * 1024 * 1024
        await self._authenticate()
        url = checked_url(self.config.url) + '/api/v1/' + path
        try:
            for attempt in range(2):
                rejected = self._access
                async with self.http.stream('GET', url, headers={'Authorization': 'Bearer ' + rejected}) as r:
                    if r.status_code == 401 and attempt == 0:
                        pass
                    else:
                        if r.status_code != 200:
                            raise BookOrbitError('bookorbit_http_' + str(r.status_code))
                        body = bytearray()
                        async for block in r.aiter_bytes(65536):
                            body.extend(block)
                            if len(body) > maximum:
                                raise BookOrbitError('bookorbit_file_too_large')
                        return bytes(body)
                await self._authenticate(force=True, rejected=rejected)
        except httpx.RequestError:
            raise BookOrbitError('bookorbit_unavailable') from None


SCHEMA = '''CREATE TABLE IF NOT EXISTS library_handoffs (
    job_id TEXT PRIMARY KEY, backend TEXT NOT NULL, sha256 TEXT NOT NULL UNIQUE,
    file_size INTEGER NOT NULL, filename TEXT NOT NULL UNIQUE, status TEXT NOT NULL,
    error TEXT, book_id INTEGER, checked_at INTEGER, created_at INTEGER NOT NULL,
    title TEXT NOT NULL DEFAULT '')'''


def phase(db_path, job_id, name):
    with connect(db_path, timeout=10) as db:
        db.execute('CREATE TABLE IF NOT EXISTS transfer_events(job_id TEXT NOT NULL,phase TEXT NOT NULL,at INTEGER NOT NULL,PRIMARY KEY(job_id,phase))')
        db.execute('INSERT OR IGNORE INTO transfer_events VALUES(?,?,?)', (job_id, name, int(time.time())))


class Handoffs:
    def __init__(self, db_path, ingest=None):
        self.path, self.ingest = db_path, Path(ingest) if ingest else None
        with connect(db_path, timeout=10) as db:
            db.execute(SCHEMA)

    def receipt(self, job_id):
        with connect(self.path, timeout=10) as db:
            db.row_factory = __import__('sqlite3').Row
            row = db.execute('SELECT * FROM library_handoffs WHERE job_id=?', (job_id,)).fetchone()
        return dict(row) if row else None

    def mark(self, job_id, status, *, error=None, book_id=None):
        with connect(self.path, timeout=10) as db:
            db.execute("UPDATE library_handoffs SET status=?,error=?,book_id=COALESCE(?,book_id),checked_at=? WHERE job_id=? AND (status!='import_confirmed' OR ?='import_confirmed')",
                       (status, error, book_id, int(time.time()), job_id, status))

    def describe(self, job):
        import sqlite3
        try:
            with connect(self.path, timeout=10) as db:
                steps = [r[0] for r in db.execute('SELECT phase FROM transfer_events WHERE job_id=? ORDER BY at,rowid', (job['id'],))]
        except sqlite3.OperationalError:
            steps = []
        receipt = self.receipt(job['id'])
        if not receipt:
            return {'backend': 'legacy' if job['status'] == 'delivered' else 'bookorbit',
                    'status': ('legacy_unverified' if job['status'] == 'delivered' else
                               'download_complete' if job['status'] == 'downloading' and 'download_complete' in steps else job['status']),
                    'steps':steps}
        status = receipt['status']
        if status == 'publishing':
            status = 'needs_review'
        if status == 'handed_off':
            dest = self.ingest / receipt['filename'] if self.ingest else None
            status = 'dock_present' if dest and dest.is_file() else 'import_unconfirmed'
        return {'backend': receipt['backend'], 'status': status, 'error': receipt['error'], 'book_id': receipt['book_id'], 'steps':steps}

    def deliver(self, job, store, staging):
        from royalbooks.jobs import AcquisitionError, MAX_BYTES, validate_epub
        import sqlite3
        job_id = job['id']
        temporary = None
        try:
            if not re.fullmatch(r'[a-f0-9]{32}', job_id):
                raise AcquisitionError('invalid_job_identity')
            previous = self.receipt(job_id)
            if previous:
                if previous['status'] in {'handed_off', 'dock_ready', 'dock_processing', 'dock_error', 'import_confirmed'}:
                    store.update(job_id, 'delivered')
                    return
                raise AcquisitionError('handoff_requires_manual_review')
            source = staging / (job_id + '.epub')
            if not source.is_file() or source.is_symlink():
                raise AcquisitionError('staged_file_missing')
            size = source.stat().st_size
            if not 0 < size <= MAX_BYTES or size != job.get('file_size') or validate_epub(source) != job.get('sha256'):
                raise AcquisitionError('staged_file_hash_mismatch')
            ingest = self.ingest
            if not ingest or not ingest.is_dir():
                raise AcquisitionError('ingest_directory_missing')
            if ingest.is_symlink():
                raise AcquisitionError('ingest_symlink_forbidden')
            data = staging.resolve()
            root = ingest.resolve()
            if root == data or root.is_relative_to(data) or data.is_relative_to(root):
                raise AcquisitionError('ingest_overlaps_staging')
            library = Config.environment().library
            if library:
                lib = Path(library).resolve()
                if root == lib or root.is_relative_to(lib) or lib.is_relative_to(root):
                    raise AcquisitionError('ingest_overlaps_library')
            filename = 'royalbooks-' + job_id + '.epub'
            dest = ingest / filename
            if dest.exists() or dest.is_symlink():
                raise AcquisitionError('ingest_filename_already_exists')
            temporary = ingest / ('.royalbooks-' + job_id + '.part')
            # Extensions excluded by BookOrbit's primary-format classifier.
            with source.open('rb') as inp, temporary.open('xb') as out:
                shutil.copyfileobj(inp, out)
                out.flush()
                os.fsync(out.fileno())
            if temporary.stat().st_size != size or validate_epub(temporary) != job['sha256']:
                raise AcquisitionError('handoff_file_changed')
            import zipfile
            from defusedxml import ElementTree
            try:
                with zipfile.ZipFile(temporary) as archive:
                    container = ElementTree.fromstring(archive.read('META-INF/container.xml'))
                    opf = next(r.attrib['full-path'] for r in container.findall('.//{*}rootfile')
                               if r.attrib.get('media-type') == 'application/oebps-package+xml')
                    if archive.getinfo(opf).file_size > 1024 * 1024:
                        raise ValueError()
                    package = ElementTree.fromstring(archive.read(opf))
                    title = (package.findtext('.//{http://purl.org/dc/elements/1.1/}title') or '')[:200]
            except Exception:
                raise AcquisitionError('invalid_epub_metadata') from None
            os.chmod(temporary, 0o660)
            with connect(self.path, timeout=10) as db:
                try:
                    db.execute('INSERT INTO library_handoffs VALUES(?,?,?,?,?,?,NULL,NULL,NULL,?,?)',
                               (job_id, 'bookorbit', job['sha256'], size, filename, 'publishing', int(time.time()), title))
                except sqlite3.IntegrityError:
                    raise AcquisitionError('duplicate_epub') from None
            phase(self.path, job_id, 'handoff_started')
            # Atomic publication WITHOUT replace: a collision can never overwrite
            # an existing book, including a concurrent publisher's file.
            os.link(temporary, dest, follow_symlinks=False)
            temporary.unlink()
            if os.name != 'nt':
                fd = os.open(ingest, os.O_RDONLY | os.O_DIRECTORY)
                try:
                    os.fsync(fd)
                finally:
                    os.close(fd)
            self.mark(job_id, 'handed_off')
            phase(self.path, job_id, 'handed_off')
            store.update(job_id, 'delivered')
            source.unlink()
        except Exception as exc:
            code = str(exc) if isinstance(exc, AcquisitionError) else type(exc).__name__
            store.update(job_id, 'needs_review', error=code[:100])
            if self.receipt(job_id):
                self.mark(job_id, 'needs_review', error=code[:100])
        finally:
            if temporary:
                temporary.unlink(missing_ok=True)

    async def verify(self, job, client: Client):
        receipt = await asyncio.to_thread(self.receipt, job['id'])
        if not receipt:
            return {'status': 'legacy_unverified' if job['status'] == 'delivered' else 'not_handed_off'}
        if receipt['status'] == 'import_confirmed':
            return {'status': 'import_confirmed', 'book_id': receipt['book_id']}
        if receipt['status'] not in {'handed_off', 'dock_ready', 'dock_processing', 'dock_error'}:
            return {'status': 'manual_review_required'}
        if not client.config.verify_import:
            return {'status': 'import_unconfirmed', 'notice': 'Importprüfung ist deaktiviert.'}
        try:
            page = await client.request('GET', 'book-dock/files', params={'search': receipt['filename'], 'limit': 100})
            rows = [r for r in page.get('items', []) if r.get('fileName') == receipt['filename']]
            if len(rows) == 1:
                status = ('dock_error' if rows[0].get('status') == 'error' else
                          'dock_ready' if rows[0].get('status') == 'ready' else 'dock_processing')
                await asyncio.to_thread(self.mark, job['id'], status, error='bookorbit_extraction_failed' if status == 'dock_error' else None)
                return {'status': status, 'dock_status': rows[0].get('status')}
            # Filename survives default finalize naming. A match is confirmed only
            # by retrieving that registered book's EPUB and hashing it, never by
            # a missing Dock row, a matching title, or BookOrbit's non-SHA fileHash.
            data = await client.request('POST', 'books/query', json={
                'q': receipt['title'] or job['id'], 'pagination': {'page': 0, 'size': 20}})
            for item in data.get('items', [])[:3]:
                ident = item.get('id')
                if type(ident) is not int:
                    continue
                payload = await client.asset(ident, 'epub')
                if len(payload) == receipt['file_size'] and hashlib.sha256(payload).hexdigest() == receipt['sha256']:
                    await asyncio.to_thread(self.mark, job['id'], 'import_confirmed', book_id=ident)
                    phase(self.path, job['id'], 'import_confirmed')
                    return {'status': 'import_confirmed', 'book_id': ident}
            await asyncio.to_thread(self.mark, job['id'], 'handed_off')
            return {'status': 'import_unconfirmed', 'notice': 'Kein identischer Bibliothekseintrag bestätigt. Keine erneute Übergabe.'}
        except BookOrbitError as exc:
            return {'status': 'import_unconfirmed', 'error': str(exc)}

    async def reconcile(self, client):
        """Bounded, independent polling; never re-submit files or finalize books."""
        while True:
            if client.config.verify_import:
                with connect(self.path, timeout=10) as db:
                    rows = db.execute("SELECT job_id FROM library_handoffs WHERE status IN ('handed_off','dock_ready','dock_processing','dock_error') AND COALESCE(checked_at,0)<? ORDER BY COALESCE(checked_at,0) LIMIT 4",
                                      (int(time.time()) - 60,)).fetchall()
                for (job_id,) in rows:
                    try:
                        async with asyncio.timeout(20):
                            await self.verify({'id': job_id, 'status': 'delivered'}, client)
                    except (BookOrbitError, TimeoutError):
                        pass
                    finally:
                        with connect(self.path, timeout=10) as db:
                            db.execute('UPDATE library_handoffs SET checked_at=? WHERE job_id=?', (int(time.time()), job_id))
            await asyncio.sleep(15)
