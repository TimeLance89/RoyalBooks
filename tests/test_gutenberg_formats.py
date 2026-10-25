"""Official RDF metadata import and first-party Gutenberg acquisition, offline fixtures only."""
from __future__ import annotations

import asyncio
import bz2
import csv
import io
import sqlite3
import tarfile
import zipfile

import httpx
import pytest

from royalbooks.app import app
from royalbooks.gutenberg_catalog import import_catalog, query_catalog
from royalbooks.gutenberg_formats import (
    format_status, import_rdf_archive, lookup_epub, parse_rdf_entry, safe_epub_uri,
)
from royalbooks.jobs import JobStore, process_queued, worker
from royalbooks.providers import Gutendex

RDF_BASE = "http://www.gutenberg.org/ebooks/"
RDF_NS = ("xmlns:rdf='http://www.w3.org/1999/02/22-rdf-syntax-ns#' "
          "xmlns:dcterms='http://purl.org/dc/terms/' "
          "xmlns:pgterms='http://www.gutenberg.org/2009/pgterms/'")


def rdf_document(ebook_id=42, formats=None):
    formats = formats if formats is not None else [
        (f"{RDF_BASE}{ebook_id}.epub.images", "application/epub+zip", 2000),
        (f"{RDF_BASE}{ebook_id}.epub.noimages", "application/epub+zip", 1000),
        (f"{RDF_BASE}{ebook_id}.txt.utf-8", "text/plain", 5000),
    ]
    pieces = []
    for url, mime, size in formats:
        pieces.append(
            f"<dcterms:hasFormat><pgterms:file rdf:about='{url}'>"
            f"<dcterms:format><rdf:Description>"
            f"<rdf:value>{mime}</rdf:value></rdf:Description></dcterms:format>"
            f"<dcterms:extent>{size}</dcterms:extent>"
            f"</pgterms:file></dcterms:hasFormat>")
    return (f"<rdf:RDF {RDF_NS}><pgterms:ebook rdf:about='ebooks/{ebook_id}'>"
            + "".join(pieces) + "</pgterms:ebook></rdf:RDF>").encode()


def make_tar(path, entries):
    with tarfile.open(path, "w:bz2") as out:
        for name, data in entries:
            member = tarfile.TarInfo(name)
            member.size = len(data)
            out.addfile(member, io.BytesIO(data))


def create_catalog(path):
    with path.open("w", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        writer.writerow(["Text#", "Type", "Issued", "Title", "Language", "Authors"])
        writer.writerow(["42", "Text", "2026-03-11", "Das Buch", "de", "Name, Anna"])


def epub_bytes():
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("mimetype", "application/epub+zip", compress_type=zipfile.ZIP_STORED)
        z.writestr("META-INF/container.xml", '<container><rootfile full-path="OPS/content.opf" media-type="application/oebps-package+xml"/></container>')
        z.writestr("OPS/content.opf", "<package/>")
    return buf.getvalue()


@pytest.fixture
def indexed(tmp_path):
    catalog = tmp_path / "pg_catalog.csv"
    db = tmp_path / "data" / "db.sqlite3"
    create_catalog(catalog)
    import_catalog(catalog, db)
    rdf = tmp_path / "rdf-files.tar.bz2"
    make_tar(rdf, [("rdf-files/cache/epub/42/pg42.rdf", rdf_document())])
    return rdf, db


def test_validates_first_party_ebook_specific_urls():
    assert safe_epub_uri("http://www.gutenberg.org/ebooks/42.epub.images", 42) == \
           "https://www.gutenberg.org/ebooks/42.epub.images"
    assert safe_epub_uri("https://www.gutenberg.org/cache/epub/42/pg42.epub", 42)
    for candidate in [
        "https://www.gutenberg.org/ebooks/43.epub",
        "https://www.gutenberg.org.evil.example/ebooks/42.epub",
        "https://127.0.0.1/cache/epub/42/pg42.epub",
        "https://www.gutenberg.org/ebooks/42.txt",
        "file:///etc/passwd",
        "https://www.gutenberg.org/ebooks/42.epub?redirect=evil",
        "https://me:pass@www.gutenberg.org/ebooks/42.epub",
    ]:
        assert safe_epub_uri(candidate, 42) is None


def test_rdf_parser_checks_mime_and_prioritizes_noimages():
    result = parse_rdf_entry(rdf_document(), 42)
    assert result == (42, "https://www.gutenberg.org/ebooks/42.epub.noimages", 1000)
    assert parse_rdf_entry(rdf_document(42, [(f"{RDF_BASE}42.epub", "text/plain", 30)]), 42) is None
    assert parse_rdf_entry(rdf_document(), 44) is None


def test_rdf_import_enables_catalog_search_with_nonverified_link(indexed):
    rdf, db = indexed
    assert not format_status(db)["indexed"]
    assert import_rdf_archive(rdf, db)["records"] == 1
    assert format_status(db)["records"] == 1
    assert lookup_epub(str(db), 42).endswith("42.epub.noimages")
    books = query_catalog(str(db), "Das Buch", "de", 5)
    assert len(books) == 1
    assert books[0].epub_url == lookup_epub(str(db), 42)
    assert books[0].availability == "official_rdf_epub_link_unverified"


def test_invalid_update_preserves_previous_rdf_metadata(indexed,tmp_path):
    rdf, db = indexed
    import_rdf_archive(rdf, db)
    broken = tmp_path / "bad.tar.bz2"
    make_tar(broken, [("rdf/42/pg42.rdf", b"<invalid")])
    with pytest.raises(Exception):
        import_rdf_archive(broken, db)
    assert format_status(db)["records"] == 1
    assert lookup_epub(str(db), 42)


def test_empty_or_non_epub_archive_rolled_back(indexed,tmp_path):
    rdf, db = indexed
    import_rdf_archive(rdf, db)
    broken = tmp_path / "no_epub.tar.bz2"
    make_tar(broken, [("rdf-files/42/pg42.rdf", rdf_document(formats=[
        ("https://www.gutenberg.org/ebooks/42.txt", "text/plain", 20)]))])
    with pytest.raises(ValueError, match="no_epub_records"):
        import_rdf_archive(broken, db)
    assert lookup_epub(str(db), 42)


@pytest.mark.asyncio
async def test_catalog_download_bypasses_blocked_gutendex_without_scraping(indexed):
    rdf, db = indexed
    import_rdf_archive(rdf, db)
    store = JobStore(str(db))
    job = store.add(42, "gutenberg_catalog")
    assert store.add(42, "gutendex")["source"] == "gutendex"
    book = epub_bytes()
    seen = []
    def handler(request):
        seen.append(str(request.url))
        assert request.url.host == "www.gutenberg.org"
        return httpx.Response(200, content=book)
    stage = db.parent / "staging"
    stage.mkdir(exist_ok=True)
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        store.claim(("queued",), "gutenberg_catalog")
        await process_queued(job, store, stage, client)
    assert len(seen) == 1
    assert store.get(job["id"])["status"] == "staged"
    assert (stage / (job["id"]+".epub")).read_bytes() == book


@pytest.mark.asyncio
async def test_blocked_gutendex_does_not_block_first_party_job(indexed, monkeypatch):
    from time import monotonic
    rdf, db = indexed
    import_rdf_archive(rdf, db)
    store = JobStore(str(db))
    independent = store.add(42, "gutenberg_catalog")
    gutendex = Gutendex()
    gutendex._blocked_until = monotonic() + 300
    async def fake_process(job, store, staging, client, provider):
        store.update(job["id"], "staged")
    monkeypatch.setattr("royalbooks.jobs.process_queued", fake_process)
    task = asyncio.create_task(worker(store, db.parent, None, gutendex))
    try:
        await asyncio.sleep(0.15)
    finally:
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    assert store.get(independent["id"])["status"] == "staged"


@pytest.mark.asyncio
async def test_api_rejects_catalog_job_before_formats_import(tmp_path, monkeypatch):
    monkeypatch.setenv("ROYALBOOKS_API_TOKEN", "testing-token")
    monkeypatch.setenv("ROYALBOOKS_ENABLE_LEGACY_WORKER", "1")
    app.state.jobs = JobStore(str(tmp_path/"db.sqlite3"))
    payload={"provider":"gutenberg_catalog","source_id":42,"rights_confirmed":True}
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        r = await client.post("/api/jobs",json=payload,headers={"X-API-Key":"testing-token"})
    assert r.status_code == 409
    assert not app.state.jobs.list()


@pytest.mark.asyncio
async def test_api_accepts_rights_confirmed_first_party_metadata(indexed,monkeypatch):
    rdf, db = indexed
    import_rdf_archive(rdf, db)
    monkeypatch.setenv("ROYALBOOKS_API_TOKEN", "testing-token")
    monkeypatch.setenv("ROYALBOOKS_ENABLE_LEGACY_WORKER", "1")
    app.state.jobs = JobStore(str(db))
    payload={"provider":"gutenberg_catalog","source_id":42,"rights_confirmed":True}
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),base_url="http://test") as client:
        assert (await client.post("/api/jobs",json=payload)).status_code == 401
        resp = await client.post("/api/jobs",json=payload,headers={"X-API-Key":"testing-token"})
    assert resp.status_code == 202
    assert resp.json()["job"]["source"] == "gutenberg_catalog"
