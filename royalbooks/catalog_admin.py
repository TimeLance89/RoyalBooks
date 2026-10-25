"""Opt-in, persistent catalog imports through the authenticated web UI.

Only two fixed official feeds are allowed. No arbitrary URL, local paths, scraping,
periodic network traffic or silent retries. Never touches Calibre metadata.db.
"""
from __future__ import annotations

import asyncio
import os
import re
import sqlite3
import tempfile
import threading
import uuid
from pathlib import Path

from royalbooks.gutenberg_catalog import download_catalog_file, import_catalog
from royalbooks.gutenberg_formats import download_archive, import_rdf_archive


class CatalogImportBusy(Exception):
    """An explicit source import is already running."""


class CatalogImportService:
    SOURCES = {"csv", "rdf"}
    ACTIVE = ("queued", "downloading", "indexing")

    def __init__(self, db_path: str):
        self.db_path = db_path
        self._lock = threading.Lock()
        self._tasks: set[asyncio.Task] = set()
        Path(db_path).parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as db:
            db.execute("""CREATE TABLE IF NOT EXISTS catalog_import_runs (
                id TEXT PRIMARY KEY,
                source TEXT NOT NULL CHECK(source IN ('csv','rdf')),
                status TEXT NOT NULL CHECK(status IN
                  ('queued','downloading','indexing','completed','failed','interrupted')),
                records INTEGER,
                error_code TEXT,
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            )""")
            # A process restart cannot claim that an interrupted download or
            # multi-minute RDF index operation completed. The index itself is
            # replaced transactionally by the importer.
            db.execute("""UPDATE catalog_import_runs SET status='interrupted',
                error_code='interrupted_by_restart',updated_at=CURRENT_TIMESTAMP
                WHERE status IN ('queued','downloading','indexing')""")

    def _connect(self):
        db = sqlite3.connect(self.db_path, timeout=20)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA busy_timeout=20000")
        return db

    def history(self) -> list[dict]:
        with self._connect() as db:
            return [dict(row) for row in db.execute(
                "SELECT * FROM catalog_import_runs ORDER BY rowid DESC LIMIT 20")]

    def request(self, source: str) -> dict:
        if source not in self.SOURCES:
            raise ValueError("unsupported_catalog_source")
        with self._lock:
            with self._connect() as db:
                db.execute("BEGIN IMMEDIATE")
                active = db.execute(
                    "SELECT 1 FROM catalog_import_runs WHERE status IN ('queued','downloading','indexing') LIMIT 1"
                ).fetchone()
                if active:
                    raise CatalogImportBusy("catalog_import_already_running")
                job_id = uuid.uuid4().hex
                db.execute("INSERT INTO catalog_import_runs(id,source,status) VALUES (?,?, 'queued')",
                           (job_id, source))
            task = asyncio.create_task(self._run(job_id, source))
            self._tasks.add(task)
            task.add_done_callback(self._tasks.discard)
            return {"id": job_id, "source": source, "status": "queued"}

    def _update(self, job_id: str, status: str, records: int | None = None, error: str | None = None):
        with self._connect() as db:
            db.execute("""UPDATE catalog_import_runs SET
                status=?,records=?,error_code=?,updated_at=CURRENT_TIMESTAMP WHERE id=?""",
                (status, records, error, job_id))

    async def _run(self, job_id: str, source: str) -> None:
        try:
            root = Path(self.db_path).parent
            # Temp files live on the persistent NAS data mount, never in RAM.
            with tempfile.TemporaryDirectory(prefix="royalbooks-import-", dir=root) as folder:
                target = Path(folder) / ("pg_catalog.csv.gz" if source == "csv" else "rdf-files.tar.bz2")
                self._update(job_id, "downloading")
                if source == "csv":
                    await download_catalog_file(target)
                else:
                    await download_archive(target)
                self._update(job_id, "indexing")
                if source == "csv":
                    records = await asyncio.to_thread(import_catalog, target, self.db_path)
                else:
                    outcome = await asyncio.to_thread(import_rdf_archive, target, self.db_path)
                    records = outcome["records"]
                self._update(job_id, "completed", records=records)
        except asyncio.CancelledError:
            self._update(job_id, "interrupted", error="cancelled")
            raise
        except Exception as exc:
            # No upstream response bodies or filesystem paths in API errors.
            detail = str(exc)
            safe = detail if re.fullmatch(r"[a-z][a-z0-9_]{0,79}", detail) else type(exc).__name__
            self._update(job_id, "failed", error=safe)

    async def close(self) -> None:
        active = list(self._tasks)
        for task in active:
            task.cancel()
        if active:
            await asyncio.gather(*active, return_exceptions=True)
