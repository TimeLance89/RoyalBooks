"""Offline provider/queue regression tests: no access to publisher servers required."""
import io
import sqlite3
import time
import zipfile

import httpx
import pytest

from royalbooks.direct_sources import (
    StandardEbooks, Wikisource, index_epub, lookup_epub, parse_standard_feed,
    safe_epub_url,
)
from royalbooks.jobs import JobStore, download_epub, process_queued


WIKI = ("https://ws-export-app.wmcloud.org/?format=epub-3&lang=de"
        "&page=Die_Verwandlung")
SE = ("https://standardebooks.org/ebooks/jane-austen/pride-and-prejudice/"
      "downloads/jane-austen_pride-and-prejudice.epub")


def test_allowlisted_source_urls_only():
    assert safe_epub_url("wikisource", WIKI)
    assert safe_epub_url("standardebooks", SE)
    unsafe = [
        "http://standardebooks.org/ebooks/test/title/downloads/book.epub",
        "https://standardebooks.org.evil.invalid/ebooks/test/title/downloads/book.epub",
        "https://standardebooks.org@evil.invalid/ebooks/test/title/downloads/book.epub",
        "https://standardebooks.org:8080/ebooks/test/title/downloads/book.epub",
        "https://standardebooks.org/feeds/opds/all",
        "https://standardebooks.org/ebooks/test/book/downloads/book.epub?token=secret",
        "https://ws-export-app.wmcloud.org/?format=pdf&lang=de&page=Something",
        "https://ws-export-app.wmcloud.org/?format=epub-3&lang=de&page=",
        "https://ws-export-app.wmcloud.org/?format=epub-3&lang=fr&page=Example",
        "https://ws-export-app.wmcloud.org/?format=epub-3&lang=de&page=Example&host=10.1.1.1",
        "https://localhost/ebooks/a.epub",
    ]
    for url in unsafe:
        assert not safe_epub_url("standardebooks", url)
        assert not safe_epub_url("wikisource", url)


def test_feed_only_explicit_public_epub_enclosures():
    feed = f"""<?xml version="1.0"?>
    <feed xmlns="http://www.w3.org/2005/Atom">
      <entry>
        <id>urn:test:book1</id><title>Pride and Prejudice</title>
        <author><name>Jane Austen</name></author>
        <link rel="enclosure" type="application/epub+zip" href="{SE}"/>
      </entry>
      <entry><id>urn:test:book2</id><title>Paywalled Book</title>
        <link rel="enclosure" type="application/epub+zip"
              href="https://premium.invalid/paid/book.epub"/></entry>
      <entry><id>urn:test:book3</id><title>Sample Book</title>
        <link rel="http://opds-spec.org/acquisition/borrow"
              type="application/epub+zip" href="{SE}"/></entry>
    </feed>""".encode()
    parsed = parse_standard_feed(feed, "Pride", 10)
    assert len(parsed) == 1
    assert parsed[0][1] == "Pride and Prejudice"
    assert parse_standard_feed(feed, "Paywalled", 10) == []
    assert parse_standard_feed(feed, "Sample", 10) == []


def test_untrusted_xml_entities_rejected():
    payload = b'<!DOCTYPE r [<!ENTITY x "hello">]><feed>&x;</feed>'
    with pytest.raises(Exception):
        parse_standard_feed(payload, "hello", 3)


def test_direct_catalog_rejects_foreign_and_expires(tmp_path):
    db = str(tmp_path / "db.sqlite3")
    with pytest.raises(ValueError):
        index_epub(db, "standardebooks", "bogus", "https://evil.invalid/book.epub")
    id1 = index_epub(db, "wikisource", "Q123", WIKI)
    assert id1 <= 2**53 - 1  # Browser Number must remain exact
    assert lookup_epub(db, "wikisource", id1) == WIKI
    assert lookup_epub(db, "standardebooks", id1) is None
    assert index_epub(db, "wikisource", "Q123", WIKI) == id1
    with sqlite3.connect(db) as conn:
        conn.execute("UPDATE direct_source_catalog SET discovered_at=?", (int(time.time())-60*60*24*9,))
    assert lookup_epub(db, "wikisource", id1) is None


@pytest.mark.asyncio
async def test_wikisource_returns_only_indexed_publisher_downloads(tmp_path):
    def response(req):
        assert req.url.host == "wsindex.toolforge.org"
        assert req.url.params["languages"] == "de"
        return httpx.Response(200, json={"results": [
            {"title": "Die Verwandlung", "wikidata_qid": "Q123",
             "authors": ["Franz Kafka"], "epub_url": WIKI,
             "ws_url": "https://de.wikisource.org/wiki/Die_Verwandlung"},
            {"title": "Die Verwandlung 2", "wikidata_qid": "Q999",
             "authors": [], "epub_url": "https://evil.invalid/book.epub"},
        ]})
    db = str(tmp_path / "db.sqlite3")
    async with httpx.AsyncClient(transport=httpx.MockTransport(response)) as client:
        found = await Wikisource(db).search(client, "Verwandlung", "de", 10)
    assert len(found) == 1
    assert found[0].epub_url == WIKI
    assert lookup_epub(db, "wikisource", int(found[0].source_id)) == WIKI


@pytest.mark.asyncio
async def test_standardebooks_public_feed_to_catalog(tmp_path):
    body = f"""<feed xmlns="http://www.w3.org/2005/Atom">
    <entry><id>urn:standardebooks:austen-book</id><title>Pride and Prejudice</title>
    <author><name>Jane Austen</name></author>
    <link rel="enclosure" type="application/epub+zip" href="{SE}"/>
    </entry></feed>""".encode()
    async with httpx.AsyncClient(transport=httpx.MockTransport(
            lambda r: httpx.Response(200, content=body))) as client:
        provider = StandardEbooks(str(tmp_path / "db.sqlite3"))
        assert await provider.search(client, "Pride", "de", 10) == []
        books = await provider.search(client, "Pride", "en", 10)
    assert len(books) == 1
    assert books[0].epub_url == SE
    assert lookup_epub(provider.db_path, "standardebooks", int(books[0].source_id)) == SE


def _epub():
    out = io.BytesIO()
    with zipfile.ZipFile(out, "w") as archive:
        archive.writestr("mimetype", b"application/epub+zip", compress_type=zipfile.ZIP_STORED)
        archive.writestr("META-INF/container.xml",
                         '<container xmlns="urn:oasis:names:tc:opendocument:xmlns:container">'
                         '<rootfiles><rootfile full-path="OPS/book.opf" media-type="application/oebps-package+xml"/>'
                         '</rootfiles></container>')
        archive.writestr("OPS/book.opf", "<package />")
    return out.getvalue()


@pytest.mark.asyncio
async def test_wikisource_direct_download_works_with_existing_queue(tmp_path):
    db = str(tmp_path / "db.sqlite3")
    ident = index_epub(db, "wikisource", "Q999", WIKI)
    store = JobStore(db)
    job = store.add(ident, "wikisource")
    staging = tmp_path / "staging"
    staging.mkdir()
    content = _epub()
    async with httpx.AsyncClient(transport=httpx.MockTransport(
            lambda req: httpx.Response(200, content=content))) as client:
        await process_queued(job, store, staging, client)
    actual = store.get(job["id"])
    assert actual["status"] == "staged"
    assert (staging / (job["id"] + ".epub")).read_bytes() == content


@pytest.mark.asyncio
async def test_direct_redirect_to_other_host_is_blocked(tmp_path):
    async with httpx.AsyncClient(transport=httpx.MockTransport(
            lambda req: httpx.Response(302, headers={"location": "https://example.com/copy.epub"}))) as client:
        with pytest.raises(Exception, match="untrusted_redirect"):
            await download_epub(client, SE, tmp_path / "book.epub", source="standardebooks")
