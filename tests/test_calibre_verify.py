"""Deterministic Calibre library verification fixtures. No running CWA required."""
import hashlib
import sqlite3

import httpx
import pytest

from royalbooks.app import app
from royalbooks.calibre_verify import verify_library_presence
from royalbooks.jobs import JobStore


def setup_library(tmp_path, book_data=b"epub bytes", relative="Author/Title (12)",
                  stem="Title", size_override=None):
    root = tmp_path / "calibre"
    root.mkdir()
    db = root / "metadata.db"
    with sqlite3.connect(db) as conn:
        conn.execute("CREATE TABLE books(id INTEGER PRIMARY KEY,title TEXT,path TEXT)")
        conn.execute("CREATE TABLE data(book INTEGER,format TEXT,uncompressed_size INTEGER,name TEXT)")
        conn.execute("INSERT INTO books VALUES(12,?,?)", ("Test Book", relative))
        conn.execute("INSERT INTO data VALUES(12,?,?,?)", ("EPUB",
            len(book_data) if size_override is None else size_override, stem))
    file = root / relative / (stem + ".epub")
    file.parent.mkdir(parents=True, exist_ok=True)
    file.write_bytes(book_data)
    return root, file


def delivered(data):
    return {"status": "delivered", "sha256": hashlib.sha256(data).hexdigest(), "file_size": len(data)}


def test_exact_epub_registered_in_calibre_database(tmp_path):
    raw = b"EPUB test data"
    root, _ = setup_library(tmp_path, raw)
    response = verify_library_presence(delivered(raw), str(root))
    assert response["status"] == "identical_epub_present"
    assert response["matches"][0]["book_id"] == 12
    assert response["matches"][0]["title"] == "Test Book"
    assert "does not establish" in response["notice"]


def test_no_match_remains_inconclusive(tmp_path):
    root, file = setup_library(tmp_path, b"same_size")
    response = verify_library_presence(delivered(b"change_it"), str(root))
    assert response["status"] == "no_exact_match_unverified"
    assert response["checked_files"] == 1
    file.write_bytes(b"different longer bytes")
    response = verify_library_presence(delivered(b"change_it"), str(root))
    assert response["status"] == "no_exact_match_unverified"
    assert response["checked_files"] == 0


def test_schema_stale_size_stays_unverified(tmp_path):
    root, _ = setup_library(tmp_path, b"book", size_override=999)
    assert verify_library_presence(delivered(b"book"), str(root))["status"] == "no_exact_match_unverified"


def test_missing_or_unreadable_library_statuses(tmp_path):
    assert verify_library_presence(delivered(b"book"), None)["status"] == "library_not_configured"
    assert verify_library_presence(delivered(b"book"), str(tmp_path / "missing"))["status"] == "library_unavailable"
    root = tmp_path / "empty"
    root.mkdir()
    assert verify_library_presence(delivered(b"book"), str(root))["status"] == "calibre_metadata_missing"
    (root / "metadata.db").write_bytes(b"not sqlite")
    assert verify_library_presence(delivered(b"book"), str(root))["status"] == "calibre_metadata_unreadable"


def test_only_delivered_and_verified_digest_are_eligible(tmp_path):
    root, _ = setup_library(tmp_path, b"book")
    assert verify_library_presence({**delivered(b"book"), "status": "staged"}, str(root))["status"] == "job_not_delivered"
    assert verify_library_presence({**delivered(b"book"), "sha256": ""}, str(root))["status"] == "missing_verified_download_digest"


def test_suspicious_metadata_paths_never_escape_library(tmp_path):
    root = tmp_path / "calibre"
    root.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "secret.epub").write_bytes(b"secret")
    with sqlite3.connect(root / "metadata.db") as conn:
        conn.executescript("""
        CREATE TABLE books(id INTEGER PRIMARY KEY,title TEXT,path TEXT);
        CREATE TABLE data(book INTEGER,format TEXT,uncompressed_size INTEGER,name TEXT);
        """)
        conn.execute("INSERT INTO books VALUES(1,'Bad','../outside')")
        conn.execute("INSERT INTO data VALUES(1,'EPUB',6,'secret')")
        conn.execute("INSERT INTO books VALUES(2,'Traversal','a')")
        conn.execute("INSERT INTO data VALUES(2,'EPUB',6,'../secret')")
    r = verify_library_presence(delivered(b"secret"), str(root))
    assert r["status"] == "no_exact_match_unverified"
    assert r["checked_files"] == 0


def test_symlinks_to_files_outside_library_ignored(tmp_path):
    root = tmp_path / "calibre"
    root.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    target = outside / "private.epub"
    target.write_bytes(b"secret")
    nested = root / "Example"
    nested.mkdir()
    try:
        (nested / "Book.epub").symlink_to(target)
    except OSError as exc:
        if getattr(exc, "winerror", None) == 1314:
            pytest.skip("Windows account lacks symlink privilege; exercised in Linux CI")
        raise
    with sqlite3.connect(root / "metadata.db") as conn:
        conn.executescript("CREATE TABLE books(id INTEGER PRIMARY KEY,title TEXT,path TEXT);"
                           "CREATE TABLE data(book INTEGER,format TEXT,uncompressed_size INTEGER,name TEXT);")
        conn.execute("INSERT INTO books VALUES(1,'Link','Example')")
        conn.execute("INSERT INTO data VALUES(1,'EPUB',6,'Book')")
    assert verify_library_presence(delivered(b"secret"), str(root))["status"] == "no_exact_match_unverified"


@pytest.mark.asyncio
async def test_api_requires_token_and_delivered_job(tmp_path, monkeypatch):
    monkeypatch.setenv("ROYALBOOKS_API_TOKEN", "secret-testing-token")
    root, _ = setup_library(tmp_path, b"book")
    monkeypatch.setenv("ROYALBOOKS_CWA_LIBRARY_PATH", str(root))
    store = JobStore(str(tmp_path / "royalbooks.sqlite3"))
    app.state.jobs = store
    job = store.add(42)
    store.update(job["id"], "delivered", sha256=hashlib.sha256(b"book").hexdigest(), file_size=4)
    url = "/api/jobs/" + job["id"] + "/library-check"
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),base_url="http://test") as client:
        assert (await client.get(url)).status_code == 401
        hdr = {"X-API-Key": "secret-testing-token"}
        assert (await client.get("/api/jobs/"+"a"*32+"/library-check",headers=hdr)).status_code == 404
        result = await client.get(url,headers=hdr)
        assert result.status_code == 200
        assert result.json()["verification"]["status"] == "legacy_unverified"
        assert (await client.get("/api/jobs/invalid/library-check",headers=hdr)).status_code == 422
