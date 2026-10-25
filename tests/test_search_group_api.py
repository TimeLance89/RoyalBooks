"""The grouped UI is strictly additional; source-specific editions remain in API items."""
import httpx
import pytest

from royalbooks.app import app
from royalbooks.providers import Book, Provider
from royalbooks.storage import MetricsStore


def edition(source: str, isbn: str | None, epub: str | None = None) -> Book:
    return Book(
        source=source, source_id=isbn or source,
        title="Harry Potter und der Feuerkelch", authors=["Rowling, J. K."],
        language="de", year=2000, cover_url=None,
        info_url="https://example.org", isbn13=isbn, epub_url=epub,
    )


class EditionProvider(Provider):
    name = "catalog"
    async def search(self, client, query, language, limit):
        return [
            edition("dnb", "9783551559122"),
            edition("dnb", "9783551559283"),
            edition("dnb", "9783551559043"),
        ]


@pytest.mark.asyncio
async def test_search_exposes_three_distinct_edition_records_and_one_display_card(tmp_path):
    old_providers = getattr(app.state, "providers", None)
    old_store = getattr(app.state, "store", None)
    try:
        app.state.providers = {"catalog": EditionProvider()}
        app.state.store = MetricsStore(str(tmp_path / "metrics.db"))
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
            response = await client.get("/api/search", params={"q": "Harry Potter und der Feuerkelch", "language": "de"})
        assert response.status_code == 200
        result = response.json()
        assert len(result["items"]) == 3
        assert [item["isbn13"] for item in result["items"]] == [
            "9783551559122", "9783551559283", "9783551559043"
        ]
        assert len(result["display_groups"]) == 1
        assert result["display_groups"][0]["item_indexes"] == [0, 1, 2]
        assert result["display_groups"][0]["epub_count"] == 0
    finally:
        if old_providers is not None:
            app.state.providers = old_providers
        if old_store is not None:
            app.state.store = old_store


def test_search_ui_has_visible_filters_and_reversible_edition_disclosure():
    from pathlib import Path
    html = (Path(__file__).parents[1] / "royalbooks" / "static" / "index.html").read_text(encoding="utf-8")
    assert 'id="search-filters"' in html
    assert 'id="search-notice"' in html
    assert "Ausgaben ansehen" in html
    assert "detail-edition-select" in html
    assert "Buchdaten · kein Downloadangebot dieser Quelle" in html
    assert "group.item_indexes.map" in html
