import httpx
import pytest

from royalbooks.providers import Gutendex, OpenLibrary, ProviderError


@pytest.mark.asyncio
async def test_gutendex_only_offers_epub_with_us_public_domain_flag():
    def handler(request):
        assert request.url.params["languages"] == "de"
        return httpx.Response(200, json={"results": [
            {"id": 12, "title": "Freies Buch", "authors": [{"name": "Autor"}], "languages": ["de"],
             "copyright": False, "formats": {"application/epub+zip": "https://www.gutenberg.org/ebooks/12.epub"}},
            {"id": 13, "title": "Unklar", "authors": [], "languages": ["de"],
             "copyright": None, "formats": {"application/epub+zip": "https://example.org/unknown.epub"}},
        ]})
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        results = await Gutendex().search(client, "test", "de", 10)
    assert len(results) == 2
    assert results[0].epub_url is not None
    assert results[1].epub_url is None


@pytest.mark.asyncio
async def test_openlibrary_metadata_only_and_language_filtering():
    async with httpx.AsyncClient(transport=httpx.MockTransport(lambda _: httpx.Response(200, json={"docs": [
        {"key": "/works/OL1W", "title": "Deutsch", "language": ["ger"], "author_name": ["K"]},
        {"key": "/works/OL2W", "title": "English", "language": ["eng"]},
        {"key": "javascript:alert(1)", "title": "Bad"},
    ]}))) as client:
        books = await OpenLibrary().search(client, "test", "de", 10)
    assert [b.title for b in books] == ["Deutsch"]
    assert books[0].language == "de"
    assert books[0].epub_url is None


@pytest.mark.asyncio
async def test_provider_errors_propagate_for_isolation():
    async with httpx.AsyncClient(transport=httpx.MockTransport(lambda _: httpx.Response(403))) as client:
        with pytest.raises(ProviderError, match="http_403"):
            await Gutendex().search(client, "test", "en", 5)
