"""Free LibGen acquisition: source binding, live-link parsing and safe transfers."""
import asyncio
import hashlib
import io
import zipfile
from urllib.parse import parse_qs

import httpx
import pytest

from royalbooks.app import app
from royalbooks.builtin_search import DirectBook, LibraryGenesis, parse_html
from royalbooks.direct_sources import index_epub, lookup_epub
from royalbooks.jobs import JobStore, process_queued, download_epub, AcquisitionError
from royalbooks.libgen_links import parse_get_link, resolve_epub, safe_catalog_url, safe_file_url
from royalbooks.providers import ProviderError

HASH = 'a'*32
CATALOG = 'https://libgen.li/ads.php?md5='+HASH
GET = 'https://libgen.li/get.php?md5='+HASH+'&key=TESTKEY123456'
CDN = 'https://cdn2.booksdl.lc/get.php?md5='+HASH+'&key=TESTKEY123456'


def epub():
    out = io.BytesIO()
    with zipfile.ZipFile(out, 'w') as archive:
        archive.writestr('mimetype', 'application/epub+zip')
        archive.writestr('META-INF/container.xml', '<container><rootfile full-path="book.opf" media-type="application/oebps-package+xml"/></container>')
        archive.writestr('book.opf', '<package/>')
    return out.getvalue()


@pytest.mark.parametrize('url', [
    'http://libgen.li/ads.php?md5='+HASH,
    'https://libgen.li.evil.example/ads.php?md5='+HASH,
    'https://127.0.0.1/ads.php?md5='+HASH,
    'https://user:secret@libgen.li/ads.php?md5='+HASH,
    CATALOG+'&redirect=https://127.0.0.1', CATALOG+'&md5='+HASH,
    CATALOG+'#fragment', CATALOG.replace('/ads.php','/edition.php'),
])
def test_only_source_bound_catalog_urls_allowed(url):
    assert not safe_catalog_url(url)


@pytest.mark.parametrize('url', [GET, CDN, CDN.replace('cdn2.', 'cdn4.'), CDN.replace('cdn2.', 'cdn5.')])
def test_pinned_https_download_routes(url):
    assert safe_file_url(url)
    assert not safe_file_url(url.replace('https:', 'http:'))
    assert not safe_file_url(url+'&redirect=evil')
    assert not safe_file_url(url.replace('/get.php', '/anything.epub'))


def test_parser_uses_actual_key_for_same_md5_and_origin_only():
    page = ('<a href="https://cdn.jsdelivr.net/style.css">CSS</a>'
            '<a href="https://evil.example/get.php?md5='+HASH+'&key=TESTKEY123456">bad</a>'
            '<a href="/get.php?md5='+('b'*32)+'&key=TESTKEY123456">wrong book</a>'
            '<a href="/get.php?md5='+HASH+'&amp;key=TESTKEY123456">GET</a>')
    assert parse_get_link(page, CATALOG) == GET
    with pytest.raises(ProviderError, match='missing'):
        parse_get_link('<h1>Verification required</h1>', CATALOG)


@pytest.mark.asyncio
async def test_resolver_does_not_follow_refusals_or_login_redirects():
    requests = []
    def handler(request):
        requests.append(request.url)
        return httpx.Response(302, headers={'location':'https://127.0.0.1/'})
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(ProviderError, match='http_302'):
            await resolve_epub(client, CATALOG)
    assert len(requests) == 1


@pytest.mark.asyncio
async def test_source_listings_get_acquisition_id_without_fake_epub_url(tmp_path, monkeypatch):
    import royalbooks.builtin_search as module
    entry = DirectBook(title='Beispiel', author='Autor', indexer='LibGen', protocol='direct_catalog',
                       language_hint='de', format_hint='EPUB', size_bytes=100,
                       score=90, source_url='https://libgen.li/edition.php?id=42',
                       publisher='', year='1900', source_id=HASH)
    async def found(*args):
        return [entry]
    monkeypatch.setattr(module, '_search_one', found)
    path = str(tmp_path/'db.sqlite3')
    async with httpx.AsyncClient() as client:
        books = await LibraryGenesis(path).search(client, 'Beispiel', 'de', 10)
    book = books[0]
    assert book.source_id == HASH  # Historic watchlist identity stays stable.
    assert book.epub_url is None   # Ads page is not falsely exposed as an EPUB file.
    assert book.as_dict()['download_supported'] is True
    assert lookup_epub(path, 'libgen', book.acquisition_id) == CATALOG


@pytest.mark.asyncio
async def test_full_libgen_queue_resolves_fresh_link_and_checks_both_hashes(tmp_path):
    content = epub()
    md5 = hashlib.md5(content).hexdigest()
    catalog = CATALOG.replace(HASH, md5)
    get = GET.replace(HASH, md5)
    cdn = CDN.replace(HASH, md5)
    path = str(tmp_path/'db.sqlite3')
    identifier = index_epub(path, 'libgen', md5, catalog)
    store = JobStore(path)
    job = store.add(identifier, 'libgen', v2_authorized=True)
    staging = tmp_path/'staging'
    staging.mkdir()
    seen = []
    def handler(request):
        seen.append(str(request.url))
        if request.url.path == '/ads.php':
            return httpx.Response(200, text=f'<a href="{get}">GET</a>')
        if request.url.host == 'libgen.li':
            return httpx.Response(307, headers={'location':cdn})
        assert request.headers['Referer'] == catalog
        return httpx.Response(200, content=content)
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        await process_queued(job, store, staging, client)
    actual = store.get(job['id'])
    assert actual['status'] == 'staged'
    assert actual['sha256'] == hashlib.sha256(content).hexdigest()
    assert actual['file_size'] == len(content)
    assert len(seen) == 3
    assert 'TESTKEY' not in str(store.list())
    assert 'TESTKEY' not in lookup_epub(path, 'libgen', identifier)


@pytest.mark.asyncio
async def test_catalog_md5_mismatch_cannot_stage_another_file(tmp_path):
    path = str(tmp_path/'db.sqlite3')
    identifier = index_epub(path, 'libgen', HASH, CATALOG)
    store = JobStore(path)
    job = store.add(identifier, 'libgen')
    staging = tmp_path/'staging';staging.mkdir()
    def handler(request):
        return (httpx.Response(200, text=f'<a href="{GET}">GET</a>')
                if request.url.path == '/ads.php' else httpx.Response(200, content=epub()))
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        await process_queued(job, store, staging, client)
    assert store.get(job['id'])['error'] == 'catalog_md5_mismatch'
    assert not list(staging.iterdir())


@pytest.mark.parametrize('target', ['https://127.0.0.1/get.php', 'http://cdn2.booksdl.lc/get.php',
                                    'https://evil.example/copy.epub', CDN.replace(HASH, 'b'*32)])
@pytest.mark.asyncio
async def test_download_redirects_cannot_leave_pinned_source_or_switch_books(tmp_path, target):
    async with httpx.AsyncClient(transport=httpx.MockTransport(
            lambda _: httpx.Response(307, headers={'location':target}))) as client:
        with pytest.raises(AcquisitionError, match='untrusted_redirect'):
            await download_epub(client, GET, tmp_path/'out.epub', source='libgen')


@pytest.mark.asyncio
async def test_libgen_api_requires_consent_and_server_indexed_identifier(tmp_path, monkeypatch):
    monkeypatch.setenv('ROYALBOOKS_API_TOKEN', 'fixture-token')
    path = str(tmp_path/'db.sqlite3')
    monkeypatch.setattr(app.state, 'jobs', JobStore(path), raising=False)
    identifier = index_epub(path, 'libgen', HASH, CATALOG)
    headers = {'X-API-Key':'fixture-token'}
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://test') as client:
        payload = {'provider':'libgen', 'source_id':identifier, 'rights_confirmed':True}
        assert (await client.post('/api/jobs', json=payload)).status_code == 401
        assert (await client.post('/api/jobs', json={**payload,'rights_confirmed':False}, headers=headers)).status_code == 400
        assert (await client.post('/api/jobs', json={**payload,'source_id':identifier+1}, headers=headers)).status_code == 409
        response = await client.post('/api/jobs', json=payload, headers=headers)
        assert response.status_code == 202
        assert response.json()['job']['v2_authorized'] == 1


def test_database_overload_is_not_misclassified_as_access_challenge():
    with pytest.raises(ValueError, match='catalogue_overloaded'):
        parse_html('<div class="alert-danger">max_user_connections exceeded</div>')


@pytest.mark.asyncio
async def test_mirror_fallback_fetches_new_key_for_same_file_and_replaces_partial(tmp_path):
    from royalbooks.jobs import download_libgen
    seen=[]
    def handler(request):
        seen.append((request.url.host,request.url.path))
        assert request.url.params['md5'] == HASH
        if request.url.path == '/ads.php':
            return httpx.Response(200,text=f'<a href="/get.php?md5={HASH}&amp;key=FRESHKEY1234">GET</a>')
        if request.url.host == 'libgen.li':return httpx.Response(500)
        assert request.url.host == 'libgen.gl'
        assert request.url.params['key'] == 'FRESHKEY1234'
        return httpx.Response(200,content=epub())
    destination=tmp_path/'download.part';destination.write_bytes(b'old partial bytes')
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        assert await download_libgen(client,CATALOG,destination) == len(epub())
    assert destination.read_bytes() == epub()
    assert seen == [('libgen.li','/ads.php'),('libgen.li','/get.php'),
                    ('libgen.gl','/ads.php'),('libgen.gl','/get.php')]


@pytest.mark.parametrize('code',[403,451,429,302])
@pytest.mark.asyncio
async def test_mirror_fallback_stops_on_refusal_throttle_or_login(tmp_path,code):
    from royalbooks.jobs import download_libgen
    seen=[]
    def handler(request):
        seen.append(request.url.host)
        return httpx.Response(code,headers={'location':'https://example.org/login'})
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(ProviderError,match=f'http_{code}'):
            await download_libgen(client,CATALOG,tmp_path/'file.part')
    assert seen == ['libgen.li']


@pytest.mark.asyncio
async def test_all_mirror_failures_preserve_transient_error(tmp_path):
    from royalbooks.jobs import download_libgen
    seen=[]
    def handler(request):
        seen.append(request.url.host)
        return httpx.Response(503 if request.url.host == 'libgen.li' else 404)
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(ProviderError,match='http_503'):
            await download_libgen(client,CATALOG,tmp_path/'file.part')
    assert len(seen) == 4


@pytest.mark.asyncio
async def test_nginx_placeholder_is_transient_not_a_missing_book():
    async with httpx.AsyncClient(transport=httpx.MockTransport(lambda _:httpx.Response(
        200,text='<html><title>Welcome to nginx!</title></html>'))) as client:
        with pytest.raises(ProviderError,match='libgen_catalog_unavailable'):
            await resolve_epub(client,CATALOG)
