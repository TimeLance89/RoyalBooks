"""Whole in-process queue-worker-handoff-library simulation.

All HTTP is mocked; the 'CWA importer' is a fixture, not a real CWA instance.
This tests wiring and persistence without touching a user's NAS.
"""
import asyncio
import hashlib
import io
import shutil
import sqlite3
import zipfile
from contextlib import suppress

import httpx
import pytest

from royalbooks import jobs
from royalbooks.calibre_verify import verify_library_presence
from royalbooks.handoff import observe_handoff
from royalbooks.jobs import JobStore, deliver_staged


def minimal_epub():
    data = io.BytesIO()
    with zipfile.ZipFile(data, "w") as z:
        z.writestr("mimetype", b"application/epub+zip", compress_type=zipfile.ZIP_STORED)
        z.writestr("META-INF/container.xml",
                   '<container xmlns="urn:oasis:names:tc:opendocument:xmlns:container">'
                   '<rootfiles><rootfile full-path="OEBPS/content.opf" '
                   'media-type="application/oebps-package+xml"/></rootfiles></container>')
        z.writestr("OEBPS/content.opf", '<package/>')
    return data.getvalue()


@pytest.mark.asyncio
async def test_actual_background_worker_through_mock_cwa_library(tmp_path, monkeypatch):
    data = tmp_path / "data"
    ingest = tmp_path / "cwa-ingest"
    library = tmp_path / "calibre-library"
    for path in (data, ingest, library):
        path.mkdir()
    store = JobStore(str(data / "jobs.sqlite3"))
    ebook = minimal_epub()
    job = store.add(42, "gutendex")

    def response(request):
        if request.url.host == "gutendex.com":
            return httpx.Response(200, json={
                "id": 42, "copyright": False,
                "formats": {"application/epub+zip": "https://www.gutenberg.org/ebooks/42.epub"}
            })
        assert request.url.host == "www.gutenberg.org"
        return httpx.Response(200, content=ebook)

    # Instantiate before monkeypatching constructor, then let the real worker own the client.
    client = httpx.AsyncClient(transport=httpx.MockTransport(response))
    monkeypatch.setattr(jobs.httpx, "AsyncClient", lambda *args, **kwargs: client)
    task = asyncio.create_task(jobs.worker(store, data, ingest))
    try:
        for _ in range(400):
            if store.get(job["id"])["status"] == "delivered":
                break
            await asyncio.sleep(0.01)
        assert store.get(job["id"])["status"] == "delivered"
    finally:
        task.cancel()
        with suppress(asyncio.CancelledError):
            await task

    sent = list(ingest.glob("*.epub"))
    assert len(sent) == 1 and sent[0].read_bytes() == ebook
    from royalbooks.bookorbit import Handoffs
    assert Handoffs(store.path,ingest).describe(store.get(job["id"]))["status"] == "dock_present"
    assert verify_library_presence(store.get(job["id"]), str(library))["status"] == "calibre_metadata_missing"

    # CWA is simulated: an independent consumer moves the file and registers it.
    lib_book = library / "Author" / "Book (1)"
    lib_book.mkdir(parents=True)
    shutil.copy2(sent[0], lib_book / "Book.epub")
    with sqlite3.connect(library / "metadata.db") as conn:
        conn.executescript("CREATE TABLE books(id INTEGER PRIMARY KEY,title TEXT,path TEXT);"
                           "CREATE TABLE data(book INTEGER,format TEXT,uncompressed_size INTEGER,name TEXT);")
        conn.execute("INSERT INTO books VALUES(1,'Book','Author/Book (1)')")
        conn.execute("INSERT INTO data VALUES(1,'EPUB',?,'Book')", (len(ebook),))
    sent[0].unlink()
    observation = observe_handoff(store.get(job["id"]), data / "staging", ingest)
    assert observation == "file_absent_unverified"
    result = verify_library_presence(store.get(job["id"]), str(library))
    assert result["status"] == "identical_epub_present"
    assert hashlib.sha256(ebook).hexdigest() == store.get(job["id"])["sha256"]
    assert result["matches"][0]["book_id"] == 1

    # Delivery remains final after a database reopening; it isn't queued again.
    reopened = JobStore(store.path)
    assert reopened.get(job["id"])["status"] == "delivered"
    assert reopened.claim(("queued",)) is None


def test_missing_cwa_folder_is_manual_review_without_data_loss(tmp_path):
    data = tmp_path / "data"
    stage = data / "staging"
    stage.mkdir(parents=True)
    store = JobStore(str(data / "jobs.sqlite3"))
    item = store.add(42)
    content = minimal_epub()
    source = stage / (item["id"] + ".epub")
    source.write_bytes(content)
    store.update(item["id"], "staged", sha256=hashlib.sha256(content).hexdigest(),
                 file_size=len(content))
    missing = tmp_path / "wrong-host-mount"
    assert not missing.exists()
    deliver_staged(store.get(item["id"]), store, stage, missing)
    result = store.get(item["id"])
    assert result["status"] == "needs_review"
    assert result["error"] == "ingest_directory_missing"
    assert source.read_bytes() == content
    assert not missing.exists()
    assert JobStore(store.path).get(item["id"])["status"] == "needs_review"
