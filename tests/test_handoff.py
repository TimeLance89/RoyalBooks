"""Restart safety and non-assertive Calibre handoff observations."""
import httpx
import pytest

from royalbooks.app import app
from royalbooks.handoff import observe_handoff
from royalbooks.jobs import JobStore, MAX_ATTEMPTS


def test_repeated_restart_does_not_bypass_attempt_limit(tmp_path):
    path = str(tmp_path / "jobs.sqlite3")
    store = JobStore(path)
    first = store.add(31)
    for attempt in range(MAX_ATTEMPTS):
        claimed = store.claim(("queued",))
        assert claimed["id"] == first["id"]
        # The worker stopped after incrementing the attempt counter.
        store = JobStore(path)
        state = store.get(first["id"])
        assert state["attempts"] == attempt + 1
        if attempt + 1 < MAX_ATTEMPTS:
            assert state["status"] == "queued"
        else:
            assert state["status"] == "failed"
            assert state["error"] == "interrupted_attempt_limit"
    assert store.claim(("queued",)) is None


def test_legacy_queued_over_cap_gets_failed(tmp_path):
    store = JobStore(str(tmp_path / "jobs.sqlite3"))
    item = store.add(21)
    with store._db() as db:
        db.execute("UPDATE jobs SET attempts=? WHERE id=?", (MAX_ATTEMPTS, item["id"]))
    assert store.claim(("queued",)) is None
    assert store.get(item["id"])["error"] == "interrupted_attempt_limit"


def test_unsupported_status_is_not_misrepresented(tmp_path):
    store = JobStore(str(tmp_path / "jobs.sqlite3"))
    item = store.add(11)
    assert observe_handoff(item, tmp_path / "staging", tmp_path / "ingest") is None


def test_staging_and_delivery_status_never_claims_import_success(tmp_path):
    store = JobStore(str(tmp_path / "jobs.sqlite3"))
    item = store.add(42)
    staging = tmp_path / "staging"
    ingest = tmp_path / "ingest"
    staging.mkdir()
    ingest.mkdir()

    store.update(item["id"], "staged")
    item = store.get(item["id"])
    assert observe_handoff(item, staging, ingest) == "staged_missing"
    (staging / (item["id"] + ".epub")).write_bytes(b"fake")
    assert observe_handoff(item, staging, ingest) == "staged_ready"

    store.update(item["id"], "delivered")
    item = store.get(item["id"])
    assert observe_handoff(item, staging, ingest) == "file_absent_unverified"
    delivered = ingest / f"gutenberg-{item['source_id']}-{item['id']}.epub"
    delivered.write_bytes(b"fake")
    assert observe_handoff(item, staging, ingest) == "awaiting_ingest"
    delivered.unlink()
    assert observe_handoff(item, staging, ingest) == "file_absent_unverified"
    assert observe_handoff(item, staging, None) == "ingest_not_configured"


def test_manual_review_prefers_existing_destination(tmp_path):
    store = JobStore(str(tmp_path / "jobs.sqlite3"))
    item = store.add(51)
    staging, ingest = tmp_path / "staging", tmp_path / "ingest"
    staging.mkdir()
    ingest.mkdir()
    store.update(item["id"], "needs_review")
    item = store.get(item["id"])
    assert observe_handoff(item, staging, ingest) == "manual_review_unknown"
    (staging / (item["id"] + ".epub")).write_bytes(b"a")
    assert observe_handoff(item, staging, ingest) == "manual_review_staged_present"
    (ingest / f"gutenberg-{item['source_id']}-{item['id']}.epub").write_bytes(b"a")
    assert observe_handoff(item, staging, ingest) == "manual_review_destination_present"


@pytest.mark.asyncio
async def test_jobs_api_includes_readonly_observation(monkeypatch, tmp_path):
    monkeypatch.setenv("ROYALBOOKS_API_TOKEN", "a-test-only-token")
    monkeypatch.setenv("ROYALBOOKS_BOOKORBIT_INGEST_PATH", str(tmp_path / "ingest"))
    (tmp_path / "ingest").mkdir()
    app.state.jobs = JobStore(str(tmp_path / "jobs.sqlite3"))
    item = app.state.jobs.add(72)
    app.state.jobs.update(item["id"], "delivered")
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        result = await client.get("/api/jobs", headers={"X-API-Key": "a-test-only-token"})
    assert result.status_code == 200
    body = result.json()
    assert body["jobs"][0]["handoff_observation"] == "file_absent_unverified"
    assert "does not confirm" in body["handoff_notice"]
