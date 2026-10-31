"""Offline container preflight tests: diagnostics never mutate CWA paths."""
import sqlite3

from royalbooks.preflight import diagnose_legacy as diagnose


def test_unconfigured_system_reports_review_not_false_success(tmp_path):
    data = tmp_path / "data"
    data.mkdir()
    report = diagnose(data_path=str(data), api_token=None)
    assert report["status"] == "review_required"
    checks = {c["name"]: c for c in report["checks"]}
    assert checks["data_mount"]["status"] == "ok"
    assert checks["cwa_ingest_mount"]["status"] == "warning"
    assert checks["queue_authentication"]["status"] == "warning"
    assert report["changes_made"] is False


def test_missing_ingest_is_error_not_created(tmp_path):
    data = tmp_path / "data"
    data.mkdir()
    missing = tmp_path / "not-the-cwa-mount"
    report = diagnose(data_path=str(data), ingest_path=str(missing), api_token="a" * 32)
    assert report["status"] == "error"
    assert not missing.exists()


def test_overlapping_ingest_and_storage_detected(tmp_path):
    data = tmp_path / "data"
    ingest = data / "ingest"
    ingest.mkdir(parents=True)
    report = diagnose(data_path=str(data), ingest_path=str(ingest))
    by_name = {x["name"]: x for x in report["checks"]}
    assert by_name["cwa_ingest_isolation"]["status"] == "error"


def test_readonly_library_check_no_writes_or_credentials(tmp_path):
    data = tmp_path / "data"
    ingest = tmp_path / "ingest"
    library = tmp_path / "library"
    for path in (data, ingest, library):
        path.mkdir()
    metadata = library / "metadata.db"
    with sqlite3.connect(metadata) as conn:
        conn.executescript("CREATE TABLE books(id INTEGER PRIMARY KEY);CREATE TABLE data(book INTEGER);")
    before = metadata.read_bytes()
    secret = "my-secret-token-should-not-appear"
    report = diagnose(data_path=str(data), ingest_path=str(ingest),
                      library_path=str(library), api_token=secret)
    after = metadata.read_bytes()
    assert before == after
    assert report["status"] == "review_required"
    checks = {x["name"]: x for x in report["checks"]}
    assert checks["cwa_ingest_mount"]["status"] == "ok"
    assert checks["calibre_metadata"]["status"] == "ok"
    assert secret not in str(report)


def test_library_missing_metadata_is_not_confirmed(tmp_path):
    data = tmp_path / "data"
    library = tmp_path / "library"
    data.mkdir()
    library.mkdir()
    result = diagnose(data_path=str(data), library_path=str(library), api_token="a" * 32)
    assert next(c for c in result["checks"] if c["name"] == "calibre_metadata")["status"] == "warning"
