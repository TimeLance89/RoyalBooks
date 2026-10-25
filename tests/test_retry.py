"""Retry hardening: failed-only, transient-only, bounded and token protected."""
from __future__ import annotations

import asyncio
import io
import sqlite3
import zipfile

import httpx
import pytest

from royalbooks.app import app
from royalbooks.jobs import (
    AcquisitionError, JobStateError, JobStore, deliver_staged,
    process_queued, validate_epub,
)


def epub_bytes():
    stream = io.BytesIO()
    with zipfile.ZipFile(stream, "w") as archive:
        archive.writestr("mimetype", "application/epub+zip", compress_type=zipfile.ZIP_STORED)
        archive.writestr(
            "META-INF/container.xml",
            '<container><rootfile full-path="OPS/book.opf" '
            'media-type="application/oebps-package+xml"/></container>',
        )
        archive.writestr("OPS/book.opf", "<package/>")
    return stream.getvalue()


def test_schema_migrates_old_database(tmp_path):
    database = str(tmp_path / "existing.sqlite3")
    with sqlite3.connect(database) as conn:
        conn.execute("""CREATE TABLE jobs (
            id TEXT PRIMARY KEY, source TEXT NOT NULL, source_id INTEGER NOT NULL,
            status TEXT NOT NULL, error TEXT, sha256 TEXT, file_size INTEGER,
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            UNIQUE(source,source_id))""")
        conn.execute("INSERT INTO jobs (id,source,source_id,status) VALUES ('old','gutendex',1,'queued')")
    store = JobStore(database)
    assert store.get("old")["attempts"] == 0
    store.claim(("queued",))
    assert store.get("old")["attempts"] == 1


def test_retry_transient_errors_and_attempt_limit(tmp_path):
    store = JobStore(str(tmp_path / "jobs.db"))
    job = store.add(70)
    with pytest.raises(JobStateError, match="retry_requires_failed_status"):
        store.retry_failed(job["id"])
    for attempt in range(1, 4):
        claimed = store.claim(("queued",))
        assert claimed["id"] == job["id"]
        store.update(job["id"], "failed", error="http_503")
        assert store.get(job["id"])["attempts"] == attempt
        if attempt < 3:
            assert store.retry_failed(job["id"])["status"] == "queued"
            with pytest.raises(JobStateError, match="retry_requires_failed_status"):
                store.retry_failed(job["id"])
        else:
            with pytest.raises(JobStateError, match="retry_limit_reached"):
                store.retry_failed(job["id"])


@pytest.mark.parametrize("error", [
    "source_not_eligible", "duplicate_epub", "invalid_epub_archive",
    "untrusted_redirect", "http_403", "no_trusted_epub",
])
def test_permanent_failures_cannot_retry(tmp_path, error):
    store = JobStore(str(tmp_path / "database.sqlite3"))
    job = store.add(99)
    store.claim(("queued",))
    store.update(job["id"], "failed", error=error)
    with pytest.raises(JobStateError, match="error_not_retryable"):
        store.retry_failed(job["id"])


def test_ambiguous_cwa_delivery_cannot_retry(tmp_path):
    store = JobStore(str(tmp_path / "database.sqlite3"))
    job = store.add(98)
    store.update(job["id"], "needs_review", error="Interrupted during ingest handoff")
    with pytest.raises(JobStateError, match="retry_requires_failed_status"):
        store.retry_failed(job["id"])


@pytest.mark.asyncio
async def test_queue_records_retryable_http_error(tmp_path):
    store = JobStore(str(tmp_path / "jobs.db"))
    job = store.add(77)
    store.claim(("queued",))
    directory = tmp_path / "staging"
    directory.mkdir()
    def handler(req):
        if req.url.host == "gutendex.com":
            return httpx.Response(200, json={
                "id":77,"copyright":False,
                "formats":{"application/epub+zip":"https://www.gutenberg.org/ebooks/77.epub"}
            })
        return httpx.Response(503)
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        await process_queued(job, store, directory, client)
    assert store.get(job["id"])["status"] == "failed"
    assert store.get(job["id"])["error"] == "http_503"
    assert store.retry_failed(job["id"])["status"] == "queued"


def test_stage_hash_verified_before_ingest(tmp_path):
    store = JobStore(str(tmp_path / "jobs.db"))
    job = store.add(88)
    stage = tmp_path / "stage"
    stage.mkdir()
    ingest = tmp_path / "ingest"
    filename = stage / (job["id"] + ".epub")
    filename.write_bytes(epub_bytes())
    store.update(job["id"], "staged", sha256="0" * 64)
    deliver_staged(store.get(job["id"]), store, stage, ingest)
    assert store.get(job["id"])["status"] == "needs_review"
    assert store.get(job["id"])["error"] == "staged_file_hash_mismatch"
    assert not list(ingest.glob("*.epub"))


def test_destination_is_not_overwritten(tmp_path):
    store = JobStore(str(tmp_path / "jobs.db"))
    job = store.add(91)
    stage = tmp_path / "stage"
    stage.mkdir()
    ingest = tmp_path / "ingest"
    ingest.mkdir()
    stage_file = stage / (job["id"] + ".epub")
    stage_file.write_bytes(epub_bytes())
    store.update(job["id"], "staged", sha256=validate_epub(stage_file))
    target = ingest / ("gutenberg-91-" + job["id"] + ".epub")
    target.write_bytes(b"do not overwrite")
    deliver_staged(store.get(job["id"]), store, stage, ingest)
    assert target.read_bytes() == b"do not overwrite"
    assert store.get(job["id"])["status"] == "needs_review"


@pytest.mark.asyncio
async def test_retry_api_requires_token_rights_and_uuid(monkeypatch,tmp_path):
    monkeypatch.setenv("ROYALBOOKS_API_TOKEN", "test-key")
    monkeypatch.setenv("ROYALBOOKS_ENABLE_LEGACY_WORKER", "1")
    app.state.jobs = JobStore(str(tmp_path / "jobs.db"))
    job = app.state.jobs.add(92)
    app.state.jobs.claim(("queued",))
    app.state.jobs.update(job["id"], "failed", error="ReadTimeout")
    url = "/api/jobs/" + job["id"] + "/retry"
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),base_url="http://test") as client:
        assert (await client.post(url,json={"rights_confirmed":True})).status_code == 401
        auth = {"X-API-Key":"test-key"}
        assert (await client.post(url,headers=auth,json={"rights_confirmed":False})).status_code == 400
        assert (await client.post("/api/jobs/invalid/retry",headers=auth,json={"rights_confirmed":True})).status_code == 422
        assert (await client.post("/api/jobs/" + "a"*32 + "/retry",headers=auth,json={"rights_confirmed":True})).status_code == 404
        assert (await client.post(url,headers=auth,json={"rights_confirmed":True})).status_code == 202
        assert (await client.post(url,headers=auth,json={"rights_confirmed":True})).status_code == 409
    assert app.state.jobs.get(job["id"])["status"] == "queued"


def test_duplicate_sha_respects_manual_review(tmp_path):
    store = JobStore(str(tmp_path / "database.sqlite3"))
    first = store.add(101)
    second = store.add(102)
    store.update(first["id"], "needs_review", sha256="x"*64)
    assert store.sha_exists("x"*64, second["id"])


def test_automatic_retry_survives_restart_and_does_not_block_ready_jobs(tmp_path, monkeypatch):
    monkeypatch.setattr('royalbooks.jobs.time.time', lambda: 1000)
    path = str(tmp_path/'jobs.db')
    store = JobStore(path)
    job = store.add(1, v2_authorized=True)
    store.claim(('queued',))
    store.update(job['id'], 'failed', error='http_503')
    assert store.schedule_retries() == 1
    assert store.get(job['id'])['retry_at'] == 1060
    assert store.schedule_retries() == 0
    store = JobStore(path)
    assert store.get(job['id'])['retry_at'] == 1060
    assert store.claim(('queued',)) is None
    ready = store.add(2, v2_authorized=True)
    assert store.claim(('queued',))['id'] == ready['id']
    monkeypatch.setattr('royalbooks.jobs.time.time', lambda: 1060)
    assert store.claim(('queued',))['id'] == job['id']
    assert store.get(job['id'])['retry_at'] is None
    store.update(job['id'], 'failed', error='ReadTimeout')
    assert store.schedule_retries() == 1
    assert store.get(job['id'])['retry_at'] == 1360
    monkeypatch.setattr('royalbooks.jobs.time.time', lambda: 1360)
    assert store.claim(('queued',))['id'] == job['id']
    store.update(job['id'], 'failed', error='http_500')
    assert store.schedule_retries() == 0
    assert store.get(job['id'])['attempts'] == 3


@pytest.mark.parametrize('status,error,authorized,attempts', [
    ('failed','http_500',False,1), ('failed','http_403',True,1),
    ('failed','invalid_epub_archive',True,1), ('failed','http_500',True,3),
    ('failed','http_500',True,0), ('needs_review','ReadTimeout',True,1),
    ('delivered','ReadTimeout',True,1),
])
def test_automatic_retry_preserves_consent_limits_and_handoffs(tmp_path,status,error,authorized,attempts):
    store = JobStore(str(tmp_path/'jobs.db'))
    job = store.add(1, v2_authorized=authorized)
    with store._db() as db:
        db.execute('UPDATE jobs SET status=?,error=?,attempts=? WHERE id=?',
                   (status,error,attempts,job['id']))
    assert store.schedule_retries() == 0
    assert store.get(job['id'])['status'] == status


@pytest.mark.asyncio
async def test_worker_schedules_retry_then_completes_handoff(tmp_path, monkeypatch):
    from royalbooks.jobs import worker
    monkeypatch.setattr('royalbooks.jobs.RETRY_DELAYS', (0,0))
    store = JobStore(str(tmp_path/'jobs.db'))
    job = store.add(7, v2_authorized=True)
    ingest = tmp_path/'ingest';ingest.mkdir()
    completed = asyncio.Event()
    def handler(req):
        if req.url.host == 'gutendex.com':
            return httpx.Response(200,json={'id':7,'copyright':False,
                'formats':{'application/epub+zip':'https://www.gutenberg.org/ebooks/7.epub'}})
        if store.get(job['id'])['attempts'] == 1:
            return httpx.Response(503)
        return httpx.Response(200, content=epub_bytes())
    original_client = httpx.AsyncClient
    monkeypatch.setattr('royalbooks.jobs.httpx.AsyncClient',
        lambda **kwargs: original_client(transport=httpx.MockTransport(handler),**kwargs))
    real_deliver = deliver_staged
    def observed_deliver(*args):
        real_deliver(*args)
        completed.set()
    monkeypatch.setattr('royalbooks.jobs.deliver_staged', observed_deliver)
    task = asyncio.create_task(worker(store,tmp_path,ingest,include_legacy=False))
    try:
        await asyncio.wait_for(completed.wait(),timeout=5)
    finally:
        task.cancel()
        with pytest.raises(asyncio.CancelledError):await task
    assert store.get(job['id'])['status'] == 'delivered'
    assert store.get(job['id'])['attempts'] == 2
    assert len(list(ingest.glob('*.epub'))) == 1
