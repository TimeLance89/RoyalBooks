import httpx
import pytest
from royalbooks.providers import GoogleBooks, isbn13


def test_isbn13_checksum():
    assert isbn13([{'type':'ISBN_13','identifier':'9780140328721'}]) == '9780140328721'
    assert isbn13(['9780140328722','invalid']) is None


@pytest.mark.asyncio
async def test_google_books_metadata_only_and_language():
    def handler(request):
        assert request.url.params['key'] == 'testkey'
        assert request.url.params['langRestrict'] == 'de'
        return httpx.Response(200,json={'items':[
            {'id':'abcd_X7','volumeInfo':{'title':'Deutsches Buch','authors':['Autor'], 'language':'de',
              'publishedDate':'2017-04','industryIdentifiers':[{'type':'ISBN_13','identifier':'9780140328721'}],
              'imageLinks':{'thumbnail':'http://books.google.com/books/content?vid=abc'}}},
            {'id':'evil/../../../etc','volumeInfo':{'title':'Nicht zulässig'}}
        ]})
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        books = await GoogleBooks('testkey').search(client,'test','de',5)
    assert len(books) == 1
    assert books[0].year == 2017
    assert books[0].isbn13 == '9780140328721'
    assert books[0].epub_url is None
    assert books[0].cover_url.startswith('https://')
