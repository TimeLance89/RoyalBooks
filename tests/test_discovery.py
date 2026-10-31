"""Provider isolation, bounded traffic, evidence and additive migration contracts."""
import asyncio
import sqlite3
from time import monotonic

import httpx
import pytest

from royalbooks.app import app, _search_windows
from royalbooks.discovery import SearchCoordinator, analyze_query, bounded_response
from royalbooks.providers import Book, Provider, ProviderError
from royalbooks.jobs import JobStore
from royalbooks.source_registry import configured_providers
from royalbooks.direct_sources import Wikisource, safe_epub_url


class Fixture(Provider):
    name = 'fixture'
    calls = 0

    async def search(self, client, query, language, limit):
        self.calls += 1
        await asyncio.sleep(.01)
        return [Book(source=self.name, source_id='1', title=query, authors=['Autor'],
                     language='de', year=None, cover_url=None, info_url='https://example.org')]


class Slow(Fixture):
    name = 'slow'

    async def search(self, *args):
        await asyncio.sleep(10)


class Broken(Fixture):
    name = 'broken'

    async def search(self, *args):
        raise RuntimeError('https://secret:password@internal/')


def test_query_normalization_and_isbn_checksum():
    query = analyze_query('  ISBN: 978-3-551-55167-2 ')
    assert query.isbn == '9783551551672'
    assert query.text == query.isbn
    assert analyze_query('9783551551673').isbn is None
    assert analyze_query('ISBN-10: 0-14-032872-6').isbn == '9780140328721'
    assert analyze_query('0140328727').isbn is None
    query = analyze_query(' Faust  author: Goethe ')
    assert query.title == 'Faust' and query.author == 'Goethe'
    assert query.variants == ('Faust author:Goethe', 'Faust')


def test_registry_has_independent_keyless_sources(monkeypatch, tmp_path):
    monkeypatch.delenv('GOOGLE_BOOKS_API_KEY', raising=False)
    monkeypatch.setenv('ROYALBOOKS_DB_PATH', str(tmp_path/'db.sqlite3'))
    names = {p.name for p in configured_providers()}
    assert {'dnb', 'openlibrary', 'wikisource', 'standardebooks', 'gutendex', 'libgen'} <= names
    assert 'google_books' not in names


@pytest.mark.asyncio
async def test_deadline_and_failure_do_not_remove_healthy_results():
    async with httpx.AsyncClient() as client:
        engine = SearchCoordinator([Slow(), Broken(), Fixture()], client, timeout=.08)
        start = monotonic()
        books, states, _ = await engine.search('Faust', 'de', 10)
        assert monotonic() - start < .5
        assert len(books) == 1
        assert {s['error'] for s in states} == {'TimeoutError', 'RuntimeError', None}
        assert 'password' not in str(states)
        assert engine.health['fixture']['health'] == 'available'


@pytest.mark.asyncio
async def test_identical_queries_share_requests_and_use_cache():
    provider = Fixture()
    async with httpx.AsyncClient() as client:
        engine = SearchCoordinator([provider], client)
        await asyncio.gather(*(engine.search('Faust', 'de', 10) for _ in range(10)))
        _, states, _ = await engine.search(' Faust ', 'de', 10)
        assert provider.calls == 1
        assert states[0]['cached'] is True
        assert len(engine.cache) == 1
        await engine.close()


@pytest.mark.asyncio
async def test_refusals_are_cooled_down():
    class Refusal(Fixture):
        async def search(self, *args):
            self.calls += 1
            raise ProviderError('http_403')
    provider = Refusal()
    async with httpx.AsyncClient() as client:
        engine = SearchCoordinator([provider], client)
        await engine.search('Faust', 'de', 1)
        _, states, _ = await engine.search('Kafka', 'de', 1)
        assert states[0]['error'] == 'source_cooldown'
        assert provider.calls == 1


@pytest.mark.asyncio
async def test_catalog_response_limit():
    async with httpx.AsyncClient(transport=httpx.MockTransport(
            lambda _: httpx.Response(200, content=b'x' * (2*1024*1024+1))),
            event_hooks={'response': [bounded_response]}) as client:
        with pytest.raises(ProviderError, match='too_large'):
            await client.get('https://example.org')


@pytest.mark.asyncio
async def test_unsupported_language_does_not_claim_remote_health():
    provider = Fixture()
    provider.supported_languages = {'en'}
    async with httpx.AsyncClient() as client:
        engine = SearchCoordinator([provider], client)
        _, states, _ = await engine.search('Faust', 'de', 1)
    assert provider.calls == 0
    assert states[0]['status'] == 'skipped'
    assert engine.health == {}


def test_metadata_and_file_claims_remain_distinct():
    base = dict(source='libgen', source_id='1', title='German lessons', authors=[],
                language=None, year=None, cover_url=None, info_url='https://example.org')
    data = Book(**base, formats=('EPUB',), language_evidence='catalog_claim_unverified').as_dict()
    assert data['language'] is None
    assert data['language_evidence'] == 'ambiguous_or_unknown'
    assert data['file_evidence'] == 'listed_unverified'
    assert data['download_supported'] is False
    metadata = Book(**{**base, 'source': 'dnb'}, isbns=('9783551551672', '9783531157887')).as_dict()
    assert len(metadata['isbns']) == 2
    assert metadata['file_evidence'] == 'metadata_only'


def test_upgrade_preserves_old_jobs_but_only_new_consent_is_claimed(tmp_path):
    path = str(tmp_path/'db.sqlite3')
    # Pre-v2 schema and an existing queued job.
    with sqlite3.connect(path) as db:
        db.execute('''CREATE TABLE jobs (id TEXT PRIMARY KEY, source TEXT, source_id INTEGER,
                   status TEXT, error TEXT, sha256 TEXT, file_size INTEGER,
                   created_at TEXT DEFAULT CURRENT_TIMESTAMP, updated_at TEXT DEFAULT CURRENT_TIMESTAMP,
                   UNIQUE(source, source_id))''')
        db.execute("INSERT INTO jobs(id,source,source_id,status) VALUES ('old','gutendex',42,'queued')")
    store = JobStore(path)
    assert store.get('old')['status'] == 'queued'
    assert store.claim(('queued',), v2_only=True) is None
    new = store.add(43, 'gutendex', v2_authorized=True)
    assert store.claim(('queued',), v2_only=True)['id'] == new['id']
    assert store.get('old')['attempts'] == 0
    assert JobStore(path).get('old')['v2_authorized'] == 0


@pytest.mark.asyncio
async def test_primary_endpoint_uses_registry_and_returns_edition_data(monkeypatch, tmp_path):
    from royalbooks.storage import MetricsStore
    _search_windows.clear()
    async with httpx.AsyncClient() as external:
        engine = SearchCoordinator([Fixture(), Broken()], external)
        monkeypatch.setattr(app.state, 'discovery', engine, raising=False)
        monkeypatch.setattr(app.state, 'store', MetricsStore(str(tmp_path/'db.sqlite3')), raising=False)
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://test') as client:
            result = await client.get('/api/books/search?q=Faust')
        assert result.status_code == 200
        data = result.json()
        assert data['language'] == 'de' and data['mode'] == 'integrated_providers'
        assert len(data['display_groups']) == 1
        assert data['search_evidence']['verified_downloads'] == 0
        assert data['items'][0]['download_supported'] is False
    _search_windows.clear()


@pytest.mark.parametrize('suffix,expected', [('&credits=false', True), ('&credits=true', False),
                                            ('&url=https://127.0.0.1', False)])
def test_wikisource_current_export_contract(suffix, expected):
    assert safe_epub_url('wikisource', 'https://ws-export-app.wmcloud.org/?format=epub-3&lang=de&page=Book'+suffix) is expected


@pytest.mark.asyncio
async def test_libgen_does_not_try_more_mirrors_after_access_refusal(monkeypatch):
    import royalbooks.builtin_search as module
    calls = []
    async def denied(client, mirror, *args):
        calls.append(mirror)
        raise ValueError('catalogue_http_403')
    monkeypatch.setattr(module, '_search_one', denied)
    async with httpx.AsyncClient() as client:
        with pytest.raises(ProviderError, match='403'):
            await module.LibraryGenesis().search(client, 'Faust', 'de', 5)
    assert len(calls) == 1


def test_same_surname_is_insufficient_to_group_different_authors():
    from royalbooks.presentation import display_groups
    base = dict(source='dnb', title='Geschichte', language='de', year=None,
                cover_url=None, info_url='https://example.org')
    books = [Book(**base, source_id='1', authors=['John Smith']),
             Book(**base, source_id='2', authors=['Jane Smith'])]
    assert len(display_groups(books)) == 2


def test_sqlite_transaction_context_releases_the_connection(tmp_path):
    from royalbooks.database import connect
    with connect(str(tmp_path/'db.sqlite3')) as connection:
        connection.execute('CREATE TABLE example (id INTEGER)')
    with pytest.raises(sqlite3.ProgrammingError, match='closed'):
        connection.execute('SELECT 1')


def test_epub_crc_corruption_is_rejected(tmp_path):
    import io
    import zipfile
    from royalbooks.jobs import validate_epub, AcquisitionError
    content = io.BytesIO()
    with zipfile.ZipFile(content, 'w') as archive:
        archive.writestr('mimetype', 'application/epub+zip')
        archive.writestr('META-INF/container.xml', '<container><rootfiles><rootfile full-path="book.opf" media-type="application/oebps-package+xml"/></rootfiles></container>')
        archive.writestr('book.opf', '<package/>')
        archive.writestr('chapter.xhtml', '<html>CRC_SENTINEL</html>')
    raw = content.getvalue().replace(b'CRC_SENTINEL', b'BAD_SENTINEL')
    path = tmp_path/'damaged.epub'
    path.write_bytes(raw)
    with pytest.raises(AcquisitionError, match='crc'):
        validate_epub(path)


@pytest.mark.asyncio
async def test_wikisource_sends_search_and_keeps_multilingual_records_ambiguous(tmp_path):
    def handler(request):
        assert request.url.params['search'] == 'Schiller'
        return httpx.Response(200, json={'results': [{
            'wikidata_qid': 'Q123', 'title': 'Schiller', 'languages': ['de','fr'],
            'ws_url': 'https://de.wikisource.org/wiki/Schiller',
            'epub_url': 'https://ws-export-app.wmcloud.org/?format=epub-3&credits=false&lang=de&page=Schiller'}]})
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        books = await Wikisource(str(tmp_path/'db.sqlite3')).search(client, 'Schiller', 'de', 5)
    assert books[0].language is None
    assert books[0].as_dict()['file_evidence'] == 'listed_unverified'
