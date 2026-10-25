"""Zero-setup direct book search is integrated, offline and deterministic."""
from __future__ import annotations

import httpx
import pytest

from royalbooks.app import app, _search_windows
from royalbooks.builtin_search import (
    MIRRORS, _language, _size_bytes, _source_url, parse_html, present,
    search_builtin,
)

MD5_A = "a" * 32
MD5_B = "b" * 32


def table(records):
    rows = []
    for id_, title, lang, ext, md5 in records:
        cells = [
            f'<td><a href="edition.php?id={id_}">{title}</a></td>',
            "<td>Testautor</td>",
            "<td>Verlag</td>",
            "<td>2020</td>",
            f"<td>{lang}</td>",
            "<td>365</td>",
            "<td>3.1 MB</td>",
            f"<td>{ext}</td>",
            f'<td><a href="get.php?md5={md5}">Quelle</a></td>',
        ]
        rows.append("<tr>" + "".join(cells) + "</tr>")
    return '<html><table id="tablelibgen">' + "".join(rows) + "</table></html>"


def test_bundled_mit_parser_reads_german_title_without_login():
    markup = table([(42, "Der Steppenwolf", "German", "epub", MD5_A)])
    rows = parse_html(markup)
    assert len(rows) == 1
    assert rows[0]["title"] == "Der Steppenwolf"
    assert rows[0]["md5"] == MD5_A
    books = present(rows, "Der Steppenwolf", "de", MIRRORS[0], 20)
    assert len(books) == 1
    assert books[0].language_hint == "de"
    assert books[0].format_hint == "EPUB"
    assert books[0].source_url == MIRRORS[0] + "edition.php?id=42"
    assert books[0].size_bytes > 3_000_000
    assert books[0].availability == "catalog_listing_unverified"


def test_german_sorting_and_language_exclusion():
    parsed = parse_html(table([
        (42, "Ein Buch", "English", "epub", MD5_A),
        (45, "Ein Buch", "Deutsch", "epub", MD5_B),
    ]))
    german = present(parsed, "Ein Buch", "de", MIRRORS[0], 10)
    assert len(german) == 1
    assert german[0].language_hint == "de"


def test_unknown_languages_not_falsely_assigned():
    assert _language("Unbekannt") is None
    assert _language("english") == "en"
    assert _language("GER") == "de"
    assert _size_bytes("NaN MB") is None
    assert _source_url(MIRRORS[0], "https://evil.example/") is None
    assert _source_url(MIRRORS[0], "edition.php?id=42#other") is None


def test_challenge_pages_are_not_reported_as_real_empty_results():
    with pytest.raises(ValueError, match="catalogue_table_missing"):
        parse_html("<html><h1>Challenge required</h1></html>")


@pytest.mark.asyncio
async def test_builtin_api_is_tokenless_and_reports_real_book_sources(monkeypatch, tmp_path):
    _search_windows.clear()
    from royalbooks.providers import Book, Provider
    from royalbooks.storage import MetricsStore
    class FakeProvider(Provider):
        name = 'fixture'
        async def search(self, client, q, language, limit):
            assert q == 'Der Steppenwolf' and language == 'de'
            return [Book(source='fixture', source_id='1', title=q, authors=['Hesse'],
                         language='de', year=1927, cover_url=None, info_url='https://example.org',
                         formats=('EPUB',))]
    monkeypatch.setattr(app.state, 'providers', {'fixture': FakeProvider()}, raising=False)
    monkeypatch.setattr(app.state, 'discovery', None, raising=False)
    app.state.store = MetricsStore(str(tmp_path/'db.sqlite3'))
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),base_url="http://test") as client:
        result = await client.get("/api/books/search",params={"q":"Der Steppenwolf","language":"de"})
    assert result.status_code == 200
    assert result.json()["mode"] == "integrated_providers"
    assert result.json()["items"][0]["formats"] == ["EPUB"]
    _search_windows.clear()


@pytest.mark.asyncio
async def test_builtins_try_fallback_mirrors_without_credentials(monkeypatch):
    import royalbooks.builtin_search as builtins
    tried = []
    async def fake_one(client, mirror, query, language, limit):
        tried.append(mirror)
        if len(tried) == 1:
            raise ValueError("catalogue_http_503")
        return present(parse_html(table([
            (11, "Faust", "German", "epub", MD5_A)
        ])), query, language, mirror, limit)
    monkeypatch.setattr(builtins, "_search_one", fake_one)
    result = await search_builtin("Faust", "de", 5)
    assert len(tried) == 2
    assert result["items"][0]["title"] == "Faust"
    assert result["sources"][0]["status"] == "unavailable"
    assert result["sources"][1]["status"] == "ok"
    assert "api_key" not in str(result)


def test_default_browser_is_no_setup_direct_search():
    from pathlib import Path
    page = (Path(__file__).parents[1] / "royalbooks" / "static" / "index.html").read_text()
    assert "Bücher suchen" in page
    assert "DIREKTSUCHE" in page
    assert "fetch('/api/books/search?'+params)" in page
    assert "Ausgabe" in page
    assert "Kein Indexer konfiguriert" not in page.split("<body>", 1)[1]
