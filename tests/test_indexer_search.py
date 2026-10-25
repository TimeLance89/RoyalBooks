"""Read-only indexer search tests; no external HTTP or download clients."""
import json

import httpx
import pytest

from royalbooks.app import app
from royalbooks.indexer_search import (
    Indexer, configured_indexers, fetch_indexer, parse_prowlarr,
    parse_rss, search_indexers, validate_url,
)


def listing(name, category=7020, **extra):
    return {
        "title": name, "indexer": "My Indexer", "protocol": "usenet",
        "size": 12345678, "categories": [{"id": category}],
        "downloadUrl": "https://example.invalid/private/download",
        "guid": "hidden-secret",
        **extra,
    }


def test_prowlarr_german_preference_without_download_urls():
    rows = [
        listing("Ein Buch German EPUB"),
        listing("Ein Buch English EPUB"),
        listing("Ein Buch EPUB"),
        listing("Ein Buch German EPUB", category=7030),
    ]
    result = parse_prowlarr(rows, "Ein Buch", "de", 10)
    assert len(result) == 2
    assert result[0].language_hint == "de"
    assert result[0].format_hint == "EPUB"
    assert result[1].language_hint is None
    assert "downloadUrl" not in str(result)
    assert "hidden-secret" not in str(result)
    assert all(x.protocol == "usenet" for x in result)


def test_torznab_rss_only_book_categories_and_no_urls():
    xml = b'''<rss xmlns:newznab="http://www.newznab.com/DTD/2010/feeds/attributes/">
       <channel>
         <item><title>Testbuch German EPUB</title>
           <link>https://example.invalid/secret.nzb</link>
           <newznab:attr name="category" value="7020"/>
           <newznab:attr name="size" value="2500000"/>
         </item>
         <item><title>Music German EPUB</title>
           <newznab:attr name="category" value="3000"/>
         </item>
       </channel></rss>'''
    config = Indexer("Meine Suche", "torznab", "https://example.org/api", "secret")
    result = parse_rss(xml, config, "Testbuch", "de", 10)
    assert len(result) == 1
    assert result[0].size_bytes == 2500000
    assert "secret.nzb" not in str(result)


@pytest.mark.parametrize("url", [
    "http://8.8.8.8:9696", "http://remote.example.org/api",
    "file:///etc/passwd", "https://user:password@host.org/api",
    "http://localhost:bad", "https://example.org/api?secret=123",
])
def test_invalid_or_unencrypted_remote_config_rejected(url):
    with pytest.raises(ValueError):
        validate_url(url, "newznab")


def test_local_prowlarr_and_https_indexers_are_supported(monkeypatch, tmp_path):
    path = tmp_path / "indexers.json"
    path.write_text(json.dumps([{
        "name": "Katalog A", "kind": "torznab",
        "url": "https://indexer.example.org/api", "api_key": "hidden-key"
    }]))
    monkeypatch.setenv("ROYALBOOKS_INDEXERS_PATH", str(path))
    monkeypatch.setenv("ROYALBOOKS_PROWLARR_URL", "http://prowlarr:9696")
    monkeypatch.setenv("ROYALBOOKS_PROWLARR_API_KEY", "hidden-prowlarr")
    configured = configured_indexers()
    assert [i.kind for i in configured] == ["prowlarr", "torznab"]
    assert "hidden-key" not in str([{"name": s.name} for s in configured])


@pytest.mark.asyncio
async def test_prowlarr_request_read_only_and_category_scoped():
    def handler(request):
        assert request.method == "GET"
        assert request.url.path == "/api/v1/search"
        assert request.url.params["categories"] == "7020"
        assert request.url.params["query"] == "Faust"
        assert request.headers["X-Api-Key"] == "secret"
        return httpx.Response(200, json=[listing("Faust German EPUB")])
    config = Indexer("Prowlarr", "prowlarr", "http://localhost:9696", "secret")
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        releases = await fetch_indexer(client, config, "Faust", "de", 10)
    assert releases[0].format_hint == "EPUB"


@pytest.mark.asyncio
async def test_token_free_search_and_absent_config_are_explicit(monkeypatch):
    monkeypatch.setenv("ROYALBOOKS_API_TOKEN", "private-api-token")
    monkeypatch.delenv("ROYALBOOKS_PROWLARR_URL", raising=False)
    monkeypatch.delenv("ROYALBOOKS_PROWLARR_API_KEY", raising=False)
    monkeypatch.setenv("ROYALBOOKS_INDEXERS_PATH", "/nonexistent/indexers.json")
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        response = await client.get("/api/indexers/search?q=Test")
    assert response.status_code == 200
    assert response.json()["configured"] is False
    assert response.json()["items"] == []


@pytest.mark.asyncio
async def test_indexer_api_returns_sanitized_search(monkeypatch):
    monkeypatch.setenv("ROYALBOOKS_API_TOKEN", "private-api-token")
    async def fake_search(query, language, limit):
        assert query == "Faust"
        assert language == "de"
        return {"configured": True, "sources": [{"name": "Test", "status": "ok", "count": 1}],
                "items": [{"title": "Faust German EPUB", "indexer": "Test",
                           "protocol": "usenet", "language_hint": "de",
                           "format_hint": "EPUB", "size_bytes": 12000, "score": 100}]}
    monkeypatch.setattr("royalbooks.app.search_indexers", fake_search)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        res = await client.get("/api/indexers/search?q=Faust")
    assert res.status_code == 200
    assert res.json()["mode"] == "read_only"
    assert "downloadUrl" not in res.text


@pytest.mark.asyncio
async def test_legacy_download_api_is_paused_by_default(monkeypatch, tmp_path):
    monkeypatch.setenv("ROYALBOOKS_API_TOKEN", "private-api-token")
    monkeypatch.delenv("ROYALBOOKS_ENABLE_LEGACY_WORKER", raising=False)
    from royalbooks.jobs import JobStore
    app.state.jobs = JobStore(str(tmp_path / "jobs.db"))
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        r = await client.post("/api/jobs", json={"provider":"gutendex","source_id":42,
                                                "rights_confirmed":True},
                              headers={"X-API-Key":"private-api-token"})
        assert r.status_code == 410
        state = await client.get("/api/jobs", headers={"X-API-Key":"private-api-token"})
        assert state.json()["legacy_worker_enabled"] is False


def test_indexer_first_browser_controls():
    from pathlib import Path
    page = (Path(__file__).parents[1] / "royalbooks" / "static" / "index.html").read_text()
    assert "Bücher suchen" in page
    assert "/api/indexers/search?" in page
    assert "releaseCard(item)" in page
    assert 'id="catalog-controls" hidden' in page

@pytest.mark.asyncio
async def test_token_free_search_has_basic_rate_limit(monkeypatch):
    from royalbooks.app import _search_windows
    import royalbooks.app as application
    _search_windows.clear()
    for _ in range(30):
        await application._limit_public_search('192.0.2.9')
    with pytest.raises(Exception) as exc:
        await application._limit_public_search('192.0.2.9')
    assert getattr(exc.value, 'status_code', None) == 429
    _search_windows.clear()


def test_search_form_has_no_token_and_admin_access_is_separate():
    from pathlib import Path
    markup = (Path(__file__).parents[1] / 'royalbooks' / 'static' / 'index.html').read_text()
    search = markup.split('<form id="search">', 1)[1].split('</form>', 1)[0]
    admin = markup.split('<details class="controls" id="admin-access">', 1)[1].split('</details>', 1)[0]
    assert 'id="token"' not in search
    assert 'id="token"' in admin
    assert "fetch('/api/books/search?'+params);" in markup
    assert "headers:{'X-API-Key':token}" not in markup.split("function releaseCard", 1)[1]



def test_german_ebook_category_kept_without_comic_confusion():
    rows = [
        listing("Hermann Hesse Steppenwolf GERMAN EPUB", category=7120),
        listing("Comics German CBZ", category=7130),
    ]
    rows_out = parse_prowlarr(rows, "Hermann Hesse", "de", 10)
    assert len(rows_out) == 1
    assert rows_out[0].language_hint == "de"


def test_german_rss_ebook_category():
    rss = b'''<rss xmlns:newznab="http://www.newznab.com/DTD/2010/feeds/attributes/">
    <channel><item><title>Goethe German EPUB</title>
    <newznab:attr name="category" value="7120"/></item></channel></rss>'''
    source = Indexer("German News", "newznab", "https://example.org/api", "private")
    result = parse_rss(rss, source, "Goethe", "de", 10)
    assert len(result) == 1


@pytest.mark.asyncio
async def test_german_search_repeats_category_query_for_prowlarr():
    def handler(req):
        assert req.url.params.get_list("categories") == ["7020", "7120"]
        return httpx.Response(200, json=[listing("Goethe German EPUB", category=7120)])
    source = Indexer("Prowlarr", "prowlarr", "http://localhost:9696", "private")
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        result = await fetch_indexer(client, source, "Goethe", "de", 10)
    assert len(result) == 1


@pytest.mark.asyncio
async def test_german_newznab_query_includes_localized_category():
    def handler(req):
        assert req.url.params["cat"] == "7020,7120"
        return httpx.Response(200, content=b'<rss><channel/></rss>')
    source = Indexer("News", "newznab", "https://example.org/api", "private")
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        assert await fetch_indexer(client, source, "Goethe", "de", 10) == []
