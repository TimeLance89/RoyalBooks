"""Store snapshots survive upstream failures; trends are genuine upstream ordering."""
import asyncio
import httpx
import pytest
from royalbooks.storefront import Storefront,parse_trending,unique_books,SHELVES
from royalbooks.providers import Book
from royalbooks.builtin_search import parse_html,present,LibraryGenesis
from royalbooks.app import app,_search_windows


def book(title='Beispiel'):
    return Book(source='libgen',source_id='a'*32,title=title,authors=['Autor'],language='de',year=2024,cover_url=None,info_url='https://libgen.li/',formats=('EPUB',),acquisition_id=123)


def trend():
    return {'works':[{'key':'/works/OL123W','title':'Tagestrend','author_name':['Autor'],'cover_i':123,'language':['eng','ger'],'first_publish_year':2024,'ebook_access':'borrowable'}]}


def test_trends_do_not_invent_german_editions_or_free_downloads():
    items=parse_trending(trend())
    assert items[0]['title']=='Tagestrend' and items[0]['language'] is None
    assert not items[0]['download_supported'] and items[0]['epub_url'] is None
    assert items[0]['source_id']=='OL123W'
    assert unique_books([book(),book()])==[book().as_dict()]

@pytest.mark.asyncio
async def test_refresh_persists_real_shelves_and_retains_last_good_on_error(tmp_path):
    calls=[]
    class Source:
        async def search_page(self,client,author,language,limit,page,mode):
            from dataclasses import replace
            return [replace(book(author),authors=[author])]
        async def latest(self,client,language,limit,topics,**kwargs):
            assert language=='de';calls.append(topics);return [book(topics)]
    async with httpx.AsyncClient(transport=httpx.MockTransport(lambda req:httpx.Response(200,json=trend()))) as client:
        store=Storefront(str(tmp_path/'db.sqlite3'),client);store.libgen=Source()
        await store.refresh();snap=store.snapshot()
        assert len(snap['shelves'])==len(SHELVES) and all(s['status']=='ready' for s in snap['shelves'])
        assert sorted(calls)==['f','l','lf','lf']  # Shelves are refreshed concurrently.
        restarted=Storefront(store.path,client)
        assert restarted.snapshot()==snap
        async def fail(*args,**kwargs):raise RuntimeError('secret URL must not leak')
        store.libgen.latest=fail
        await store.refresh_one('recent')
        cached=next(s for s in store.snapshot()['shelves'] if s['id']=='recent')
        assert cached['items'] and cached['stale'] and cached['status']=='cached'
        assert 'secret' not in str(store.snapshot())
        store.libgen=Source()
        async def empty(*args,**kwargs):return []
        store.libgen.latest=empty
        await store.refresh_one('fiction')
        assert next(s for s in store.snapshot()['shelves'] if s['id']=='fiction')['items']

@pytest.mark.asyncio
async def test_preview_uses_pinned_isbn_and_work_paths_and_caches(tmp_path):
    seen=[]
    def handler(req):
        seen.append(str(req.url))
        if '/isbn/' in req.url.path:return httpx.Response(200,json={'works':[{'key':'/works/OL123W'}]})
        return httpx.Response(200,json={'description':{'value':'<p>Inhalt &amp; Geschichte</p>'}})
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        store=Storefront(str(tmp_path/'db.sqlite3'),client)
        first=await store.preview(isbn='9783551551672')
        assert first['description'].strip()=='Inhalt & Geschichte'
        assert (await store.preview(isbn='9783551551672'))['cached']
        assert len(seen)==2 and all(url.startswith('https://openlibrary.org/') for url in seen)
    async with httpx.AsyncClient(transport=httpx.MockTransport(lambda req:httpx.Response(500))) as client:
        restored=Storefront(store.path,client)
        assert (await restored.preview(isbn='9783551551672'))['description']==first['description']

@pytest.mark.asyncio
async def test_foreign_work_reference_is_not_followed(tmp_path):
    seen=[]
    def handler(req):seen.append(req.url);return httpx.Response(200,json={'works':[{'key':'https://127.0.0.1/private'}]})
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        store=Storefront(str(tmp_path/'db.sqlite3'),client)
        assert (await store.preview(isbn='9783551551672'))['description'] is None
    assert len(seen)==1

@pytest.mark.asyncio
async def test_store_and_preview_api_need_no_token_but_reject_arbitrary_urls(tmp_path,monkeypatch):
    _search_windows.clear()
    async with httpx.AsyncClient(transport=httpx.MockTransport(lambda req:httpx.Response(200,json={'description':'Beschreibung'}))) as upstream:
        store=Storefront(str(tmp_path/'db.sqlite3'),upstream);store.save('releases',[book().as_dict()])
        monkeypatch.setattr(app.state,'storefront',store,raising=False)
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),base_url='http://test') as client:
            assert (await client.get('/api/store')).json()['shelves'][0]['items']
            assert (await client.get('/api/book-preview?work=OL123W')).json()['description']=='Beschreibung'
            for path in ['/api/book-preview','/api/book-preview?work=https://evil.example/','/api/book-preview?isbn=9783551551672&work=OL123W']:
                assert (await client.get(path)).status_code==422


def test_cover_enabled_libgen_rows_keep_correct_columns_and_isbns():
    md5='a'*32
    html='<table id="tablelibgen"><tr><td><img src="/covers/1000/'+md5+'_small.jpg"></td><td><a href="edition.php?id=1">Titel</a><a href="edition.php?id=1">9783551551672</a></td><td>Autor</td><td>Verlag</td><td>2024</td><td>German</td><td>100</td><td>1 MB</td><td>epub</td><td><a href="ads.php?md5='+md5+'">GET</a></td></tr></table>'
    rows=parse_html(html);books=present(rows,'Titel','de','https://libgen.li/',10)
    assert len(books)==1 and books[0].title=='Titel' and books[0].format_hint=='EPUB'
    assert books[0].cover_url=='https://libgen.li/covers/1000/'+md5+'_small.jpg'
    assert books[0].isbns==('9783551551672',)
    rows[0]['cover_path']='https://evil.example/cover.jpg'
    assert present(rows,'Titel','de','https://libgen.li/',10)[0].cover_url is None

@pytest.mark.asyncio
async def test_latest_catalog_requests_real_time_sort_not_publication_claim(tmp_path):
    requests=[]
    def handler(req):
        requests.append(req)
        assert req.url.params['order']=='time_added' and req.url.params['ordermode']=='desc'
        assert req.url.params['req'].strip() in {'lang:ger ext:epub','lang:ger'}
        assert req.url.params.get_list('topics[]')==['f']
        return httpx.Response(200,text='<table id="tablelibgen"></table>')
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        assert await LibraryGenesis(str(tmp_path/'db.sqlite3')).latest(client,topics='f')==[]
    assert requests
    assert requests[0].url.params['req'].strip()=='lang:ger ext:epub'
