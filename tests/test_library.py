"""Calibre shelf permissions, pagination, Unicode search and file containment."""
import sqlite3
from pathlib import Path
import httpx
import pytest
from royalbooks.library import bookshelf, library_file, LibraryUnavailable
from royalbooks.app import app

@pytest.fixture
def library(tmp_path):
    root=tmp_path/'library';root.mkdir()
    with sqlite3.connect(root/'metadata.db') as db:
        db.executescript('CREATE TABLE books(id INTEGER PRIMARY KEY,title TEXT,sort TEXT,author_sort TEXT,timestamp TEXT,path TEXT);CREATE TABLE data(book INTEGER,format TEXT,name TEXT);')
        for i,title,author in [(1,'Über den Wolken','Müller'),(2,'Faust','Goethe'),(3,'Zwei Bücher','Zweig')]:
            db.execute('INSERT INTO books VALUES(?,?,?,?,?,?)',(i,title,title,author,f'2026-10-0{i}',f'Author/Book ({i})'))
            db.execute('INSERT INTO data VALUES(?,?,?)',(i,'EPUB','Book'))
            directory=root/f'Author/Book ({i})';directory.mkdir(parents=True)
            (directory/'Book.epub').write_bytes(b'test epub')
            (directory/'cover.jpg').write_bytes(b'test cover')
    return root


def test_shelf_search_paging_and_read_only(library):
    before=(library/'metadata.db').read_bytes()
    data=bookshelf(str(library),limit=2)
    assert [b['id'] for b in data['items']]==[3,2]
    assert data['total']==3 and data['has_more']
    assert [b['id'] for b in bookshelf(str(library),offset=2)['items']]==[1]
    assert bookshelf(str(library),query='ÜBER')['items'][0]['id']==1
    assert bookshelf(str(library),query='MÜLLER')['items'][0]['id']==1
    assert bookshelf(str(library),query='%')['items']==[]
    assert [b['id'] for b in bookshelf(str(library),sort='title')['items']]==[2,3,1]
    assert bookshelf(str(library),sort='author')['items'][0]['id']==2
    assert str(library) not in str(data)
    assert (library/'metadata.db').read_bytes()==before


def test_files_missing_and_path_traversal(library,tmp_path):
    assert library_file(str(library),1,'epub').read_bytes()==b'test epub'
    assert library_file(str(library),1,'cover').read_bytes()==b'test cover'
    assert library_file(str(library),99,'cover') is None
    (tmp_path/'cover.jpg').write_bytes(b'private')
    with sqlite3.connect(library/'metadata.db') as db:
        db.execute("UPDATE books SET path='../' WHERE id=1")
        db.execute("UPDATE data SET name='../secret' WHERE book=2")
    assert library_file(str(library),1,'cover') is None
    assert library_file(str(library),2,'epub') is None
    with pytest.raises(LibraryUnavailable):bookshelf(None)
    (library/'metadata.db').write_bytes(b'invalid')
    with pytest.raises(LibraryUnavailable):bookshelf(str(library))


def test_symlink_cannot_escape(library,tmp_path):
    target=library/'Author/Book (1)/cover.jpg';target.unlink()
    secret=tmp_path/'private.jpg';secret.write_bytes(b'private')
    try:target.symlink_to(secret)
    except OSError:pytest.skip('Symlink privilege unavailable')
    assert library_file(str(library),1,'cover') is None

@pytest.mark.asyncio
async def test_library_api_authentication_and_file_access(library,monkeypatch):
    monkeypatch.setenv('ROYALBOOKS_LIBRARY_BACKEND','calibre-readonly')
    monkeypatch.setenv('ROYALBOOKS_CWA_LIBRARY_PATH',str(library))
    monkeypatch.setenv('ROYALBOOKS_API_TOKEN','test-library-key')
    monkeypatch.delenv('ROYALBOOKS_TRUSTED_NETWORKS',raising=False)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),base_url='http://test') as client:
        assert (await client.get('/api/library')).status_code==401
        assert (await client.get('/api/library/1/epub')).status_code==401
        headers={'X-API-Key':'test-library-key'}
        r=await client.get('/api/library',headers=headers)
        assert r.status_code==200 and r.json()['total']==3
        assert (await client.get('/api/library?sort=invalid',headers=headers)).status_code==422
        r=await client.get('/api/library/1/epub',headers=headers)
        assert r.content==b'test epub' and 'attachment' in r.headers['content-disposition']
        assert (await client.get('/api/library/1/cover',headers=headers)).headers['content-type']=='image/jpeg'
        assert (await client.get('/api/library/99/epub',headers=headers)).status_code==404
        assert (await client.get('/api/library/1/metadata.db',headers=headers)).status_code==422
