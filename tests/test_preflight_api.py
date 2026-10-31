"""The UI-facing preflight must be authenticated, read-only and non-disclosing."""
import sqlite3

import httpx
import pytest

from royalbooks.app import app


@pytest.mark.asyncio
async def test_preflight_api_protected_and_read_only(monkeypatch, tmp_path):
    data = tmp_path / "data"
    ingest = tmp_path / "ingest"
    library = tmp_path / "library"
    for path in (data, ingest, library):
        path.mkdir()
    with sqlite3.connect(library / "metadata.db") as db:
        db.executescript("CREATE TABLE books(id INTEGER); CREATE TABLE data(book INTEGER);")
    before = (library / "metadata.db").read_bytes()
    token = "super-secret-test-token-with-sufficient-length"
    monkeypatch.setenv("ROYALBOOKS_API_TOKEN", token)
    monkeypatch.setenv("ROYALBOOKS_DB_PATH", str(data / "royalbooks.sqlite3"))
    monkeypatch.setenv("ROYALBOOKS_BOOKORBIT_INGEST_PATH", str(ingest))
    monkeypatch.setenv("ROYALBOOKS_BOOKORBIT_LIBRARY_PATH", str(library))
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        assert (await client.get("/api/preflight")).status_code == 401
        assert (await client.get("/api/preflight", headers={"X-API-Key":"invalid"})).status_code == 401
        response = await client.get("/api/preflight", headers={"X-API-Key":token})
    assert response.status_code == 200
    report = response.json()
    assert report["status"] == "review_required"
    assert report["changes_made"] is False
    assert {c["name"] for c in report["checks"]} >= {"data_mount","bookorbit_ingest_mount","bookorbit_library_mount"}
    assert (library / "metadata.db").read_bytes() == before
    assert not (data / "royalbooks.sqlite3").exists()
    assert token not in str(report)
    assert str(tmp_path) not in str(report)


@pytest.mark.asyncio
async def test_preflight_missing_mount_reports_error_without_creating_it(monkeypatch, tmp_path):
    data = tmp_path / "data"
    data.mkdir()
    missing = tmp_path / "not-an-ingest-directory"
    monkeypatch.setenv("ROYALBOOKS_DB_PATH",str(data / "royalbooks.sqlite3"))
    monkeypatch.setenv("ROYALBOOKS_BOOKORBIT_INGEST_PATH",str(missing))
    monkeypatch.delenv("ROYALBOOKS_BOOKORBIT_LIBRARY_PATH",raising=False)
    monkeypatch.setenv("ROYALBOOKS_API_TOKEN","test-token-longer-than-24-chars")
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),base_url="http://test") as client:
        response=await client.get("/api/preflight",headers={"X-API-Key":"test-token-longer-than-24-chars"})
    assert response.status_code == 200
    assert response.json()["status"] == "error"
    assert not missing.exists()


@pytest.mark.asyncio
async def test_preflight_api_disabled_without_configured_auth(monkeypatch):
    monkeypatch.delenv("ROYALBOOKS_API_TOKEN",raising=False)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),base_url="http://test") as client:
        assert (await client.get("/api/preflight",headers={"X-API-Key":"any"})).status_code == 503
