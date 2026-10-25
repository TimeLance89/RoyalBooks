"""Artwork only comes from catalog-listed pinned URLs and bounded JPEG responses."""
import httpx
import pytest
from royalbooks.artwork import register_artwork,read_artwork,fetch_artwork,safe_artwork

MD5='a'*32
URL='https://libgen.li/covers/1000/'+MD5+'_small.jpg'

@pytest.mark.parametrize('url',[URL.replace('https:','http:'),URL+'?next=evil',URL.replace('libgen.li','libgen.li.evil.example'),URL.replace(MD5,'b'*32),URL.replace('/covers/','/../'),URL.replace('https://','https://user:pass@')])
def test_artwork_rejects_untrusted_locations(url):
    assert not safe_artwork(MD5,url)

@pytest.mark.asyncio
async def test_artwork_uses_source_referer_then_persistent_cache(tmp_path):
    path=str(tmp_path/'db.sqlite3');register_artwork(path,MD5,URL);seen=[]
    content=b'\xff\xd8\xff'+b'jpeg-fixture'
    def handler(req):
        seen.append(req.url)
        assert req.headers['referer']=='https://libgen.li/'
        assert str(req.url)==URL.replace('_small.jpg','.jpg')
        return httpx.Response(200,content=content)
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        assert await fetch_artwork(path,MD5,client)==content
        assert await fetch_artwork(path,MD5,client)==content
        assert await fetch_artwork(path,'b'*32,client) is None
    assert len(seen)==1 and read_artwork(path,MD5)[1]==content

@pytest.mark.parametrize('content',[b'',b'<html>not an image</html>',b'\xff\xd8\xff'+b'x'*(512*1024)],ids=['empty','html','too-large'])
@pytest.mark.asyncio
async def test_artwork_rejects_empty_non_jpeg_and_oversized_responses(tmp_path,content):
    path=str(tmp_path/'db.sqlite3');register_artwork(path,MD5,URL)
    async with httpx.AsyncClient(transport=httpx.MockTransport(lambda req:httpx.Response(200,content=content))) as client:
        assert await fetch_artwork(path,MD5,client) is None
    assert read_artwork(path,MD5)[1] is None

@pytest.mark.asyncio
async def test_artwork_does_not_follow_redirects(tmp_path):
    path=str(tmp_path/'db.sqlite3');register_artwork(path,MD5,URL);seen=[]
    def handler(req):seen.append(req.url);return httpx.Response(302,headers={'location':'https://evil.invalid/cover.jpg'})
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        assert await fetch_artwork(path,MD5,client) is None
    assert len(seen)==1


@pytest.mark.asyncio
async def test_existing_thumbnail_cache_upgrades_without_catalog_refresh(tmp_path):
    from royalbooks.database import connect
    path=str(tmp_path/'db.sqlite3');register_artwork(path,MD5,URL)
    thumbnail=b'\xff\xd8\xffold-thumbnail';original=b'\xff\xd8\xfforiginal-cover'
    with connect(path) as db:
        db.execute('UPDATE catalog_artwork SET url=?,content=? WHERE md5=?',(URL,thumbnail,MD5))
    seen=[]
    def handler(req):
        seen.append(str(req.url));return httpx.Response(200,content=original)
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        assert await fetch_artwork(path,MD5,client)==original
        register_artwork(path,MD5,URL)
        assert await fetch_artwork(path,MD5,client)==original
    assert seen==[URL.replace('_small.jpg','.jpg')]
    assert read_artwork(path,MD5)==(URL.replace('_small.jpg','.jpg'),original)
