"""Verified anonymous acquisition: hostile redirects, metadata and restart safety."""
import io
import sqlite3
import time
import zipfile

import httpx
import pytest

from royalbooks.availability import Availability
from royalbooks.direct_sources import lookup_epub, safe_epub_url
from royalbooks.jobs import JobStore, deliver_staged, process_queued, validate_epub
from royalbooks.providers import ProviderError
from royalbooks.webshare_source import Webshare, cached, inspect_epub, remember, safe_file


def epub(language='de', title='Die Seelenburg', broken_order=False, extra=None):
    buf = io.BytesIO()
    entries = [
        ('mimetype', 'application/epub+zip'),
        ('META-INF/container.xml', '<container><rootfiles><rootfile full-path="content.opf" media-type="application/oebps-package+xml"/></rootfiles></container>'),
        ('content.opf', f'<package><metadata xmlns:dc="http://purl.org/dc/elements/1.1/">'
         f'<dc:title>{title}</dc:title><dc:creator>Jason Dark</dc:creator><dc:language>{language}</dc:language>'
         '</metadata></package>'),
        ('chapter.xhtml', '<html><body>A book chapter</body></html>')]
    if broken_order:
        entries = entries[1:] + entries[:1]
    if extra:
        entries.append(extra)
    with zipfile.ZipFile(buf, 'w') as archive:
        for name, body in entries:
            archive.writestr(name, body, compress_type=zipfile.ZIP_DEFLATED if name == 'bomb' else zipfile.ZIP_STORED)
    return buf.getvalue()


def search_xml(files):
    return '<response><status>OK</status>' + ''.join(
        f'<file><ident>{ident}</ident><name>{name}</name><size>1000</size><password>0</password></file>'
        for ident, name in files) + '</response>'


def test_repair_preserves_content_and_rejects_unsafe_archives(tmp_path):
    original = epub(broken_order=True)
    fixed, info, digest = inspect_epub(original)
    assert info['language'] == 'de' and info['title'] == 'Die Seelenburg'
    with zipfile.ZipFile(io.BytesIO(original)) as before, zipfile.ZipFile(io.BytesIO(fixed)) as after:
        assert after.infolist()[0].filename == 'mimetype'
        assert {n: before.read(n) for n in before.namelist()} == {n: after.read(n) for n in after.namelist()}
    path = tmp_path / 'fixed.epub'
    path.write_bytes(fixed)
    assert validate_epub(path) == digest
    with pytest.warns(UserWarning, match='Duplicate name'):
        duplicate = epub(extra=('chapter.xhtml', 'duplicate'))
    for data in [b'captcha html', epub(extra=('../unsafe', 'payload')), duplicate,
                 epub(extra=('bomb', 'a' * 2_000_000))]:
        with pytest.raises(ProviderError):
            inspect_epub(data)


def test_repair_hash_does_not_depend_on_download_time(monkeypatch):
    original = epub(broken_order=True)
    monkeypatch.setattr(zipfile.time, 'localtime', lambda *_: (2026, 10, 31, 10, 0, 0, 0, 0, 0))
    first = inspect_epub(original)
    monkeypatch.setattr(zipfile.time, 'localtime', lambda *_: (2027, 1, 1, 12, 0, 0, 0, 0, 0))
    second = inspect_epub(original)
    assert first[0] == second[0] and first[2] == second[2]


@pytest.mark.asyncio
async def test_search_actual_language_relevance_and_cached_job(tmp_path):
    db = str(tmp_path / 'db.sqlite')
    calls = []
    files = [('German1234', 'Jason Dark - Die Seelenburg.epub'),
             ('Czech12345', 'Jason Dark - Die Seelenburg.epub'),
             ('Unrelated1', 'Other writer - Die Nilbraut.epub')]

    def handler(request):
        calls.append(str(request.url))
        if request.url.path == '/api/search/':
            return httpx.Response(200, text=search_xml(files))
        if request.url.path == '/api/file_link/':
            german = b'ident=German1234' in request.content
            return httpx.Response(200, text='<response><status>OK</status><link>https://free.17.dl.wsfiles.cz/'
                                  + ('de' if german else 'cs') + '?secret=shortlived</link></response>')
        return httpx.Response(200, content=epub('cs' if request.url.path == '/cs' else 'de', broken_order=True))

    provider = Webshare(db)
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        books = await provider.search(client, 'Die Seelenburg', 'de', 30)
        assert len(books) == 1
        book = books[0]
        assert book.language == 'de' and book.authors == ['Jason Dark']
        assert book.as_dict()['download_supported']
        assert book.as_dict()['file_evidence'] == 'verified_epub_cached'
        assert 'secret' not in str(book.as_dict()) and book.epub_url is None
        before = len(calls)
        assert len(await provider.search(client, 'Die Seelenburg', 'de', 30)) == 1
        assert len(calls) == before
        # New provider instance reuses exact verified bytes across a restart.
        assert len(await Webshare(db).search(client, 'Die Seelenburg', 'de', 30)) == 1
        assert len(calls) == before + 1  # Only the search; neither file fetched again.
        store = JobStore(db)
        job = store.add(book.acquisition_id, 'webshare', v2_authorized=True)
        job = store.claim(('queued',))
        stage = tmp_path / 'stage';stage.mkdir()
        await process_queued(job, store, stage, client)
        assert store.get(job['id'])['status'] == 'staged'
        assert len(calls) == before + 1
        availability = Availability(db)
        assert await availability.probe('webshare', book.acquisition_id) > 0
        ingest = tmp_path / 'ingest';ingest.mkdir()
        deliver_staged(store.get(job['id']), store, stage, ingest)
        assert store.get(job['id'])['status'] == 'delivered'
        assert validate_epub(next(ingest.glob('*.epub'))) == store.get(job['id'])['sha256']


@pytest.mark.asyncio
async def test_redirect_never_leaves_source_and_no_filenames_as_metadata(tmp_path):
    seen = []

    def handler(request):
        seen.append(str(request.url))
        if request.url.path == '/api/search/':
            return httpx.Response(200, text=search_xml([('German1234', 'Die Seelenburg.epub')]))
        if request.url.path == '/api/file_link/':
            return httpx.Response(200, text='<response><status>OK</status><link>https://free.17.dl.wsfiles.cz/file</link></response>')
        return httpx.Response(302, headers={'Location': 'http://127.0.0.1/secret'})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        assert await Webshare(str(tmp_path / 'db')).search(client, 'Die Seelenburg', 'de', 30) == []
    assert not any('127.0.0.1' in url for url in seen)

    def wrong_title(request):
        if request.url.path == '/api/search/':
            return httpx.Response(200, text=search_xml([('German1234', 'Die Seelenburg.epub')]))
        if request.url.path == '/api/file_link/':
            return httpx.Response(200, text='<response><status>OK</status><link>https://free.17.dl.wsfiles.cz/file</link></response>')
        return httpx.Response(200, content=epub(title='Something Else'))

    async with httpx.AsyncClient(transport=httpx.MockTransport(wrong_title)) as client:
        assert await Webshare(str(tmp_path / 'db2')).search(client, 'Die Seelenburg', 'de', 30) == []


def test_cache_bounds_expiry_corruption_and_source_identity(tmp_path, monkeypatch):
    import royalbooks.webshare_source as source
    db = str(tmp_path / 'cache.sqlite')
    assert cached(db, 1) is None and not (tmp_path / 'cache.sqlite').exists()
    monkeypatch.setattr(source, 'MAX_RECORDS', 1)
    payload, info, digest = inspect_epub(epub())
    first = remember(db, 'German1234', payload, info, digest)
    second = remember(db, 'German5678', payload, info, digest)
    assert cached(db, first) is None or cached(db, second) is None
    with sqlite3.connect(db) as conn:
        record = conn.execute('SELECT record_id FROM webshare_verified').fetchone()[0]
        conn.execute('UPDATE webshare_verified SET checked_at=?', (int(time.time()) - 90000,))
    assert cached(db, record) is None
    record = remember(db, 'German9999', payload, info, digest)
    with sqlite3.connect(db) as conn:
        conn.execute('UPDATE webshare_verified SET epub=?', (b'changed bytes',))
    assert cached(db, record) is None
    assert safe_epub_url('webshare', lookup_epub(db, 'webshare', record))
    assert not safe_epub_url('webshare', 'https://webshare.cz/#/file/../secret')
    for url in ['http://free.17.dl.wsfiles.cz/file', 'https://free.17.dl.wsfiles.cz.evil.test/file',
                'https://user:password@free.17.dl.wsfiles.cz/file', 'https://free.17.dl.wsfiles.cz:8080/file']:
        assert not safe_file(url)
