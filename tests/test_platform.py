"""File-only offers, bounded real probes, preferences and author/ISBN routing."""
import asyncio,time
import httpx
import pytest
from royalbooks.availability import Availability
from royalbooks.direct_sources import index_epub
from royalbooks.settings import PlatformSettings
from royalbooks.app import app,_search_windows
from royalbooks.discovery import SearchCoordinator,analyze_query
from royalbooks.providers import Book,Provider
from royalbooks.builtin_search import LibraryGenesis
from royalbooks.dnb import DNB
from royalbooks.providers import OpenLibrary

HASH='a'*32
CATALOG='https://libgen.li/ads.php?md5='+HASH
GET='https://libgen.li/get.php?md5='+HASH+'&key=TESTKEY123456'
CDN='https://cdn4.booksdl.lc/get.php?md5='+HASH+'&key=TESTKEY123456'

@pytest.mark.asyncio
async def test_probe_follows_only_same_file_and_reads_zip_prefix(tmp_path):
    path=str(tmp_path/'db.sqlite3');record=index_epub(path,'libgen',HASH,CATALOG)
    service=Availability(path);seen=[]
    def handler(req):
        seen.append(req)
        if req.url.path=='/ads.php':return httpx.Response(200,text='<a href="'+GET+'">GET</a>')
        assert req.headers['Range']=='bytes=0-1023' and req.headers['Referer']==CATALOG
        if req.url.host=='libgen.li':return httpx.Response(307,headers={'Location':CDN})
        return httpx.Response(206,headers={'Content-Range':'bytes 0-1023/123456'},content=b'PK\x03\x04'+b'x'*1020)
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        service.client=client;assert await service.probe('libgen',record)==123456
    assert len(seen)==3

@pytest.mark.parametrize('location',[CDN.replace(HASH,'b'*32),'https://127.0.0.1/private.epub','https://evil.example/get.php?md5='+HASH+'&key=TESTKEY123456'])
@pytest.mark.asyncio
async def test_probe_rejects_wrong_md5_and_foreign_redirects(tmp_path,location):
    path=str(tmp_path/'db.sqlite3');record=index_epub(path,'libgen',HASH,CATALOG);service=Availability(path);seen=[]
    def handler(req):
        seen.append(req.url)
        return httpx.Response(200,text='<a href="'+GET+'">GET</a>') if req.url.path=='/ads.php' else httpx.Response(307,headers={'Location':location})
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        service.client=client
        with pytest.raises(ValueError,match='untrusted_redirect'):await service.probe('libgen',record)
    assert len(seen)==2

@pytest.mark.asyncio
async def test_html_is_not_offered_as_an_epub(tmp_path):
    path=str(tmp_path/'db.sqlite3');url='https://library.memoryoftheworld.org/files/test/Author/Book/book.epub';record=index_epub(path,'memoryoftheworld','uuid',url);service=Availability(path)
    async with httpx.AsyncClient(transport=httpx.MockTransport(lambda req:httpx.Response(200,content=b'<html>login</html>'))) as client:
        service.client=client
        with pytest.raises(ValueError,match='not_epub_zip'):await service.probe('memoryoftheworld',record)

@pytest.mark.asyncio
async def test_check_requests_coalesce_and_cache_survives_restart(tmp_path):
    service=Availability(str(tmp_path/'db.sqlite3'));count=[]
    async def probe(*args):count.append(args);return 1000
    service.probe=probe
    task=asyncio.create_task(service.worker());service.tasks=[task]
    try:
        await asyncio.gather(*(service.request([('libgen',123)]) for _ in range(8)))
        await asyncio.wait_for(service.queue.join(),2)
        assert len(count)==1
        restored=Availability(service.path)
        result=await restored.request([('libgen',123)])
        assert result[0]['status']=='available' and restored.queue.empty()
    finally:await service.close()

@pytest.mark.asyncio
async def test_settings_persist_without_credentials_and_validate_sources(tmp_path,monkeypatch):
    settings=PlatformSettings(str(tmp_path/'db.sqlite3'));monkeypatch.setattr(app.state,'preferences',settings,raising=False)
    monkeypatch.setenv('ROYALBOOKS_API_TOKEN','settings-test-key');monkeypatch.delenv('ROYALBOOKS_TRUSTED_NETWORKS',raising=False)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),base_url='http://test') as c:
        assert (await c.get('/api/settings')).status_code==401
        headers={'X-API-Key':'settings-test-key'}
        r=await c.put('/api/settings',headers=headers,json={'sources':['libgen','dnb'],'language':'en','available_only':True})
        assert r.status_code==200 and 'settings-test-key' not in r.text
        assert PlatformSettings(settings.path).get()['sources']==['libgen','dnb']
        assert (await c.put('/api/settings',headers=headers,json={'sources':['unknown']})).status_code==422
        assert (await c.put('/api/settings',headers=headers,json={'sources':[]})).status_code==422
        assert (await c.put('/api/settings',headers=headers,json={'result_limit':999})).status_code==422

@pytest.mark.parametrize('query',['EAN 978-3-551-55167-2','IAN:9783551551672','ISBN-10 0-14-032872-6'])
def test_book_ean_isbn_variants(query):assert analyze_query(query).isbn is not None

@pytest.mark.asyncio
async def test_author_fields_are_sent_to_the_catalog_not_just_free_text(tmp_path):
    path=str(tmp_path/'db.sqlite3');seen=[]
    def handler(req):
        seen.append(req)
        if req.url.host=='libgen.li':
            assert req.url.params.get_list('columns[]')==['a']
            assert req.url.params['page']=='2'
            return httpx.Response(200,text='<table id="tablelibgen"></table>')
        if req.url.host=='services.dnb.de':
            assert 'per all "Stephen King"' in req.url.params['query']
            assert req.url.params['startRecord']=='26'
            return httpx.Response(200,content=b'<records/>')
        assert req.url.params['author']=='Stephen King' and req.url.params['offset']=='25'
        return httpx.Response(200,json={'docs':[]})
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as c:
        await LibraryGenesis(path).search_page(c,'Stephen King','de',25,2,'author')
        await DNB().search_page(c,'Stephen King','de',25,2,'author')
        await OpenLibrary().search_page(c,'Stephen King','de',25,2,'author')

@pytest.mark.asyncio
async def test_author_mode_excludes_books_only_about_an_author():
    class Source(Provider):
        name='fixture'
        async def search(self,*args):
            return [Book(source=self.name,source_id='1',title='Es',authors=['King, Stephen'],language='de',year=None,cover_url=None,info_url='https://example.org'),Book(source=self.name,source_id='2',title='Stephen King Biography',authors=['Different Author'],language='de',year=None,cover_url=None,info_url='https://example.org')]
    async with httpx.AsyncClient() as c:
        coordinator=SearchCoordinator([Source()],c)
        items,_,_=await coordinator.search('Stephen King','de',25,'author')
        assert [b.title for b in items]==['Es']
        await coordinator.close()

@pytest.mark.asyncio
async def test_own_section_urls_work_on_reload():
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),base_url='http://test') as c:
        for path in ['/discover','/search','/authors','/library','/downloads','/settings','/settings/sources']:
            r=await c.get(path);assert r.status_code==200 and '<title>RoyalBooks' in r.text

def test_isbn_work_fallback_requires_same_title_author_and_language():
    from royalbooks.app import same_work
    from dataclasses import replace
    reference=Book(source='dnb',source_id='1',title='Es : Roman',authors=['King, Stephen'],language='de',year=1997,cover_url=None,info_url='https://d-nb.info/1')
    candidate=replace(reference,source='libgen',title='Es',authors=['Stephen King'])
    assert same_work(reference,candidate)
    assert not same_work(reference,replace(candidate,title='Es muss sein'))
    assert not same_work(reference,replace(candidate,authors=['Different Author']))
    assert not same_work(reference,replace(candidate,language='en'))

@pytest.mark.asyncio
async def test_isbn_resolution_keeps_other_editions_explicit(monkeypatch):
    from dataclasses import replace
    from royalbooks.app import _search
    reference=Book(source='dnb',source_id='1',title='Es : Roman',authors=['King, Stephen'],language='de',year=1997,cover_url=None,info_url='https://d-nb.info/1',isbn13='9783551551672')
    candidate=Book(source='libgen',source_id='a'*32,title='Es',authors=['Stephen King'],language='de',year=2020,cover_url=None,info_url='https://libgen.li/',formats=('EPUB',),acquisition_id=123)
    class Source: name='libgen';purpose='file_listing'
    class Engine:
        providers=[Source()]
        async def search(self,value,language,limit,mode='all',page=1,selected=None):
            from royalbooks.discovery import analyze_query
            if analyze_query(value).isbn:return [reference],[{'provider':'dnb','status':'found','results':1}],analyze_query(value)
            return [candidate,replace(candidate,source_id='b'*32,authors=['Different Author']),replace(candidate,source_id='c'*32,language='en')],[{'provider':'libgen','status':'found','results':3}],analyze_query(value)
    monkeypatch.setattr(app.state,'discovery',Engine(),raising=False);monkeypatch.setattr(app.state,'store',None,raising=False)
    data=await _search('9783551551672','de',25,'isbn')
    assert data['resolved_work']==reference.title
    files=[b for b in data['items'] if b['source']=='libgen']
    assert len(files)==1 and files[0]['edition_match']=='same_work_other_edition'
    assert files[0]['isbns']==[]  # Never copy a paper edition ISBN onto a different EPUB.
