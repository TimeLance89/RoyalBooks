"""Offline, deterministic user-triggered catalog manager tests."""
import asyncio
import sqlite3

import httpx
import pytest

from royalbooks.app import app
from royalbooks.catalog_admin import CatalogImportBusy, CatalogImportService


async def wait_idle(manager: CatalogImportService):
    for _ in range(200):
        if not manager._tasks:
            return
        await asyncio.sleep(0.01)
    pytest.fail("catalog import task did not terminate")


@pytest.mark.asyncio
async def test_csv_import_from_ui_reaches_completed(monkeypatch, tmp_path):
    manager = CatalogImportService(str(tmp_path / "service.sqlite3"))
    downloaded = []
    imported = []
    async def download(path):
        downloaded.append(path.name)
        path.write_bytes(b"fixture archive")
    def do_import(path, db_path):
        assert path.is_file()
        imported.append(path.name)
        return 123
    monkeypatch.setattr("royalbooks.catalog_admin.download_catalog_file", download)
    monkeypatch.setattr("royalbooks.catalog_admin.import_catalog", do_import)
    result = manager.request("csv")
    assert result["status"] == "queued"
    await wait_idle(manager)
    assert downloaded == ["pg_catalog.csv.gz"]
    assert imported == ["pg_catalog.csv.gz"]
    assert manager.history()[0]["status"] == "completed"
    assert manager.history()[0]["records"] == 123
    assert not list(tmp_path.glob("royalbooks-import-*"))
    await manager.close()


@pytest.mark.asyncio
async def test_one_import_at_a_time_and_restart_requires_manual_action(monkeypatch, tmp_path):
    gate = asyncio.Event()
    async def blocked_download(path):
        await gate.wait()
        path.write_bytes(b"fixture archive")
    monkeypatch.setattr("royalbooks.catalog_admin.download_archive", blocked_download)
    monkeypatch.setattr("royalbooks.catalog_admin.import_rdf_archive", lambda path, db: {"records": 7})
    path = str(tmp_path / "service.sqlite3")
    manager = CatalogImportService(path)
    manager.request("rdf")
    with pytest.raises(CatalogImportBusy):
        manager.request("csv")
    gate.set()
    await wait_idle(manager)
    assert manager.history()[0]["status"] == "completed"
    await manager.close()
    with sqlite3.connect(path) as db:
        db.execute("INSERT INTO catalog_import_runs(id,source,status) VALUES ('dangling','csv','indexing')")
    after_restart = CatalogImportService(path)
    assert after_restart.history()[0]["status"] == "interrupted"
    assert after_restart.history()[0]["error_code"] == "interrupted_by_restart"
    assert not list(tmp_path.glob("royalbooks-import-*"))
    await after_restart.close()


@pytest.mark.asyncio
async def test_download_failure_does_not_claim_indexed(monkeypatch, tmp_path):
    async def offline(path):
        raise httpx.ConnectTimeout("contains untrusted path /etc/config")
    monkeypatch.setattr("royalbooks.catalog_admin.download_catalog_file", offline)
    manager = CatalogImportService(str(tmp_path / "service.sqlite3"))
    manager.request("csv")
    await wait_idle(manager)
    row = manager.history()[0]
    assert row["status"] == "failed"
    assert row["error_code"] == "ConnectTimeout"
    assert "/etc/config" not in str(row)
    await manager.close()


@pytest.mark.asyncio
async def test_catalog_management_requires_token(monkeypatch, tmp_path):
    token = "use-a-long-server-side-only-secret-value"
    monkeypatch.setenv("ROYALBOOKS_API_TOKEN", token)
    manager = CatalogImportService(str(tmp_path / "service.sqlite3"))
    app.state.catalog_imports = manager
    gate = asyncio.Event()
    async def download(path):
        await gate.wait()
        path.write_bytes(b"small fixture")
    monkeypatch.setattr("royalbooks.catalog_admin.download_catalog_file", download)
    monkeypatch.setattr("royalbooks.catalog_admin.import_catalog", lambda path, db: 3)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        payload = {"source": "csv"}
        assert (await client.get("/api/catalog/imports")).status_code == 401
        assert (await client.post("/api/catalog/imports", json=payload)).status_code == 401
        assert (await client.post("/api/catalog/imports", json={"source": "arbitrary"},headers={"X-API-Key":token})).status_code == 422
        response = await client.post("/api/catalog/imports", json=payload,headers={"X-API-Key":token})
        assert response.status_code == 202
        second = await client.post("/api/catalog/imports",json={"source":"rdf"}, headers={"X-API-Key":token})
        assert second.status_code == 409
        history = await client.get("/api/catalog/imports",headers={"X-API-Key":token})
        assert history.status_code == 200
        assert history.json()["imports"][0]["source"] == "csv"
        assert token not in str(history.json())
    gate.set()
    await wait_idle(manager)
    await manager.close()


@pytest.mark.asyncio
async def test_optional_rdf_import_and_failure_codes(monkeypatch, tmp_path):
    async def download(path):
        path.write_bytes(b"rdf archive placeholder")
    monkeypatch.setattr("royalbooks.catalog_admin.download_archive", download)
    monkeypatch.setattr("royalbooks.catalog_admin.import_rdf_archive",lambda file, db:{"records":45})
    manager=CatalogImportService(str(tmp_path/"test.sqlite3"))
    manager.request("rdf")
    await wait_idle(manager)
    assert manager.history()[0]["records"] == 45
    await manager.close()
