"""Broader catalog coverage without dropping source binding or language evidence."""
import io
import zipfile
from urllib.parse import parse_qs
import httpx
import pytest
from royalbooks.builtin_search import _search_one
from royalbooks.discovery import analyze_query
from royalbooks.direct_sources import lookup_epub, safe_epub_url
from royalbooks.jobs import JobStore, process_queued, download_epub, AcquisitionError
from royalbooks.memory_links import listed_url, safe_memory_url
from royalbooks.memory_source import MemoryOfTheWorld
from royalbooks.providers import ProviderError


def table(start,count=25,extension='epub'):
    rows=[]
    for i in range(start,start+count):
        rows.append(f'<tr><td><a href="edition.php?id={i}">Buch {i}</a></td><td>Autor</td><td>Verlag</td><td>2024</td><td>German</td><td>100</td><td>1 MB</td><td>{extension}</td><td><a href="ads.php?md5={i:032x}">GET</a></td></tr>')
    return '<table id="tablelibgen">'+''.join(rows)+'</table>'

@pytest.mark.asyncio
async def test_libgen_filters_upstream_and_reads_real_25_record_pages():
    seen=[]
    def handler(req):
        params=req.url.params;seen.append(params)
        assert params['req']=='Autor lang:ger ext:epub'
        assert params['res']=='25'
        page=int(params.get('page','1'))
        return httpx.Response(200,text=table((page-1)*25+1))
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        items=await _search_one(client,'https://libgen.li/','Autor','de',60)
    assert len(items)==60 and len(seen)==3
    assert all(b.format_hint=='EPUB' and b.language_hint=='de' for b in items)

@pytest.mark.asyncio
async def test_libgen_no_epub_keeps_other_file_formats():
    def handler(req):
        return httpx.Response(200,text=table(1,0) if 'ext:epub' in req.url.params['req'] else table(1,1,'pdf'))
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        items=await _search_one(client,'https://libgen.li/','Autor','de',30)
    assert len(items)==1 and items[0].format_hint=='PDF'

@pytest.mark.asyncio
async def test_libgen_repeated_page_is_not_a_large_catalog():
    calls=[]
    def handler(req):calls.append(req.url);return httpx.Response(200,text=table(1))
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        items=await _search_one(client,'https://libgen.li/','Autor','de',60)
    assert len(items)==25 and len(calls)==2


def test_reported_king_spelling_correction_and_structured_query():
    q=analyze_query(' Steven King es ')
    assert q.text=='Stephen King es' and q.original_text=='Steven King es'
    q=analyze_query('Es author:Steven King')
    assert q.title=='Es' and q.author=='Stephen King'
    assert analyze_query('Steven Universe').text=='Steven Universe'
    assert analyze_query('Es author: Stephen King').original_text is None


@pytest.mark.asyncio
async def test_libgen_isbn_queries_use_the_isbn_column():
    def handler(req):
        assert req.url.params.get_list('columns[]') == ['i']
        assert req.url.params['req']=='9783551551672 lang:ger ext:epub'
        return httpx.Response(200,text=table(1,1))
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        assert len(await _search_one(client,'https://libgen.li/','9783551551672','de',30))==1


UUID='d416538a-9495-5369-81df-84ea057b6f11'
ROW={'_id':UUID,'title':'Ein Buch','authors':['Autor'], 'languages':['deu'],
     'library_url':'/files/test/','cover_url':'Autor/Ein Buch (1)/cover.jpg',
     'formats':[{'format':'epub','dir_path':'Autor/Ein Buch (1)/','file_name':'Ein Buch.epub'}],
     'pubdate':'2024-01-01','abstract':'<p>Beschreibung</p>', 'identifiers':[{'scheme':'isbn','code':'9783551551672'}]}


def test_memory_paths_remain_pinned_and_source_listed():
    url=listed_url('/files/test/','Autor/Ein Buch (1)/','Ein Buch.epub')
    assert safe_memory_url(url) and safe_epub_url('memoryoftheworld',url)
    assert not safe_epub_url('standardebooks',url)
    for bad in [url.replace('https:','http:'),url.replace('library.memoryoftheworld.org','127.0.0.1'),url+'?next=http://localhost',url.replace('/files/test/','/files/test/%2e%2e/'),url.replace('/files/test/','/files/test/%252e%252e/'),url.replace('.epub','.html'),url.replace('/files/','/private/'),url.replace('https://','https://user:password@')]:
        assert not safe_memory_url(bad),bad
    assert listed_url('https://evil.invalid/','Author/','Book.epub') is None
    assert listed_url('/files/test/','../../private/','Book.epub') is None

@pytest.mark.asyncio
async def test_memory_lists_only_matching_languages_with_trusted_epubs(tmp_path):
    rows=[ROW,{**ROW,'_id':'a416538a-9495-5369-81df-84ea057b6f11','languages':['eng']},
          {**ROW,'_id':'b416538a-9495-5369-81df-84ea057b6f11','library_url':'https://127.0.0.1/'},
          {**ROW,'_id':'c416538a-9495-5369-81df-84ea057b6f11','languages':['deu','eng']}]
    provider=MemoryOfTheWorld(str(tmp_path/'db.sqlite3'))
    async with httpx.AsyncClient(transport=httpx.MockTransport(lambda req:httpx.Response(200,json={'_items':rows}))) as client:
        books=await provider.search(client,'Ein Buch','de',30)
    assert len(books)==1
    book=books[0];assert book.language=='de' and book.as_dict()['download_supported']
    assert book.isbns==('9783551551672',) and book.description.strip()=='Beschreibung'
    assert lookup_epub(provider.db_path,'memoryoftheworld',book.acquisition_id)==book.epub_url
    assert lookup_epub(provider.db_path,'libgen',book.acquisition_id) is None
    assert book.cover_url.endswith('/cover.jpg')

@pytest.mark.asyncio
async def test_memory_pagination_preserves_query_and_deduplicates(tmp_path):
    seen=[]
    provider=MemoryOfTheWorld(str(tmp_path/'db.sqlite3'));provider.min_interval=0
    def handler(req):
        assert req.url.params['q']=='Ein Buch'
        seen.append(req.url.params['offset'])
        if req.url.params['offset']=='0':return httpx.Response(200,json={'_items':[{**ROW,'languages':['eng']}]*48,'_meta':{'total':49}})
        return httpx.Response(200,json={'_items':[ROW]})
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        books=await provider.search(client,'Ein Buch','de',30)
    assert len(books)==1 and seen==['0','48']

@pytest.mark.asyncio
async def test_memory_refusal_and_malformed_catalog_are_errors(tmp_path):
    provider=MemoryOfTheWorld(str(tmp_path/'db.sqlite3'))
    async with httpx.AsyncClient(transport=httpx.MockTransport(lambda req:httpx.Response(403))) as client:
        with pytest.raises(ProviderError,match='403'):await provider.search(client,'Buch','de',30)
    provider=MemoryOfTheWorld(str(tmp_path/'db.sqlite3'))
    async with httpx.AsyncClient(transport=httpx.MockTransport(lambda req:httpx.Response(200,json={'_items':'invalid'}))) as client:
        with pytest.raises(ProviderError,match='invalid'):await provider.search(client,'Buch','de',30)

@pytest.mark.asyncio
async def test_memory_real_queue_path_validates_epub_and_blocks_redirect(tmp_path):
    path=str(tmp_path/'db.sqlite3');provider=MemoryOfTheWorld(path)
    async with httpx.AsyncClient(transport=httpx.MockTransport(lambda req:httpx.Response(200,json={'_items':[ROW]}))) as client:
        book=(await provider.search(client,'Buch','de',30))[0]
    store=JobStore(path);job=store.add(book.acquisition_id,'memoryoftheworld',v2_authorized=True)
    staging=tmp_path/'staging';staging.mkdir()
    buffer=io.BytesIO()
    with zipfile.ZipFile(buffer,'w') as z:
        z.writestr('mimetype','application/epub+zip')
        z.writestr('META-INF/container.xml','<container><rootfile full-path="book.opf" media-type="application/oebps-package+xml"/></container>')
        z.writestr('book.opf','<package/>')
    async with httpx.AsyncClient(transport=httpx.MockTransport(lambda req:httpx.Response(200,content=buffer.getvalue()))) as client:
        await process_queued(job,store,staging,client)
    assert store.get(job['id'])['status']=='staged'
    async with httpx.AsyncClient(transport=httpx.MockTransport(lambda req:httpx.Response(302,headers={'location':'https://evil.example/book.epub'}))) as client:
        with pytest.raises(AcquisitionError,match='untrusted_redirect'):
            await download_epub(client,book.epub_url,tmp_path/'bad.epub',source='memoryoftheworld')
