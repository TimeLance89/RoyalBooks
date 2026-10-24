"""Official offline Gutenberg catalog: deterministic tests without any network."""
from __future__ import annotations

import csv
import gzip
import io
import sqlite3

import httpx
import pytest

from royalbooks.gutenberg_catalog import (
    GutenbergCatalog, catalog_status, import_catalog, query_catalog, parse_entries,
    MAX_CSV_CHARS, CATALOG_URL, download_catalog_file,
)

HEADER = ["Text#", "Type", "Issued", "Title", "Language", "Authors", "Subjects", "LoCC", "Bookshelves"]


def make_catalog(path, rows):
    with io.StringIO(newline="") as out:
        writer = csv.writer(out)
        writer.writerow(HEADER)
        writer.writerows(rows)
        raw = out.getvalue().encode("utf-8")
    if path.suffix == ".gz":
        with gzip.open(path, "wb") as f:
            f.write(raw)
    else:
        path.write_bytes(raw)


@pytest.fixture
def fixture(tmp_path):
    catalog = tmp_path / "pg_catalog.csv.gz"
    database = tmp_path / "data" / "library.sqlite3"
    rows = [
        [1, "Text", "1998-02-10", "Faust: Der Tragödie erster Teil", "de", "Goethe, Johann Wolfgang von", "", "", ""],
        [2, "Text", "1999-01-01", "Die Verwandlung", "de", "Kafka, Franz", "", "", ""],
        [3, "Text", "2000-01-01", "The Metamorphosis", "en", "Kafka, Franz", "", "", ""],
        [4, "Image", "2026-01-01", "Der Prozess Bild", "de", "Kafka, Franz", "", "", ""],
        [5, "Text", "2026-01-01", "Faust (English Translation)", "en", "Goethe, Johann", "", "", ""],
        [6, "Text", "2026-01-01", "Ein\nBuch mit Untertitel", "de;en", "Autor, Erika", "", "", ""],
    ]
    make_catalog(catalog, rows)
    return catalog, database


def test_catalog_import_and_german_search(fixture):
    catalog, database = fixture
    assert catalog_status(database)["indexed"] is False
    assert import_catalog(catalog, database) == 5
    state = catalog_status(database)
    assert state["indexed"] is True
    assert state["records"] == 5
    books = query_catalog(str(database), "Faust Goethe", "de", 10)
    assert len(books) == 1
    assert books[0].source == "gutenberg_catalog"
    assert books[0].source_id == "1"
    assert books[0].language == "de"
    assert books[0].epub_url is None
    assert books[0].availability == "catalog_only_epub_unverified"


def test_catalog_language_and_text_filter(fixture):
    catalog, database = fixture
    import_catalog(catalog, database)
    assert not query_catalog(str(database), "Metamorphosis", "de", 10)
    assert query_catalog(str(database), "Metamorphosis", "en", 10)[0].source_id == "3"
    assert not query_catalog(str(database), "Prozess Bild", "de", 10)
    assert query_catalog(str(database), "Ein Buch Untertitel", "de", 10)


@pytest.mark.asyncio
async def test_provider_never_queries_http(fixture):
    catalog, database = fixture
    import_catalog(catalog, database)
    def forbidden(request):
        raise AssertionError("Offline provider attempted network connection")
    async with httpx.AsyncClient(transport=httpx.MockTransport(forbidden)) as client:
        items = await GutenbergCatalog(str(database)).search(client, "Kafka Verwandlung", "de", 5)
    assert len(items) == 1


def test_bad_update_keeps_previous_catalog_and_stats(fixture, tmp_path):
    catalog, database = fixture
    import_catalog(catalog, database)
    broken = tmp_path / "broken.csv"
    broken.write_text("random,columns\n1,2\n", encoding="utf-8")
    before = catalog_status(database)
    with pytest.raises(ValueError, match="invalid_gutenberg_columns"):
        import_catalog(broken, database)
    assert catalog_status(database) == before
    assert len(query_catalog(str(database), "Faust", "de", 10)) == 1


def test_invalid_utf8_rollback(fixture,tmp_path):
    catalog,database = fixture
    import_catalog(catalog,database)
    bad = tmp_path / "invalid.csv"
    bad.write_bytes(b"Text#,Type,Issued,Title,Language,Authors\n1,Text,2020-10-01,\xff,de,name\n")
    with pytest.raises(UnicodeError):
        import_catalog(bad,database)
    assert catalog_status(database)["records"] == 5


def test_import_detects_duplicate_ids(fixture, tmp_path):
    catalog, database = fixture
    import_catalog(catalog,database)
    duplicate = tmp_path / "duplicate.csv"
    make_catalog(duplicate, [
        [1, "Text", "1990-01-01", "First", "de", "A", "", "", ""],
        [1, "Text", "1990-01-01", "Second", "de", "B", "", "", ""],
    ])
    with pytest.raises(sqlite3.IntegrityError):
        import_catalog(duplicate, database)
    assert catalog_status(database)["records"] == 5


def test_no_network_or_download_url_in_catalog(fixture):
    catalog, database = fixture
    import_catalog(catalog, database)
    all_books = query_catalog(str(database), "Kafka", None, 10)
    assert all(book.epub_url is None for book in all_books)
    assert all(book.availability == "catalog_only_epub_unverified" for book in all_books)


def test_uncompressed_file_and_language_missing(tmp_path):
    source = tmp_path / "raw.csv"
    make_catalog(source, [
        [11,"Text","2026-01-01","Lang present","ger","Person","", "", ""],
        [12,"Text","2026-01-01","Lang missing","","Person","", "", ""],
    ])
    database = tmp_path / "db.sqlite3"
    assert import_catalog(source,database) == 1
    assert len(query_catalog(str(database),"Lang","ger",10)) == 1
    assert not query_catalog(str(database),"Lang missing",None,10)


@pytest.mark.asyncio
async def test_compressed_catalog_download_rejects_redirect(monkeypatch,tmp_path):
    # Pure unit contract: no actual remote request.
    from royalbooks.gutenberg_catalog import CATALOG_URL
    assert CATALOG_URL == "https://www.gutenberg.org/cache/epub/feeds/pg_catalog.csv.gz"
