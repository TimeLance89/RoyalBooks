"""Host-side Docker audit unit tests do not require Docker or a NAS."""
from importlib.util import module_from_spec, spec_from_file_location
from pathlib import Path

module_path = Path(__file__).parents[1] / "scripts" / "nas_cwa_audit.py"
spec = spec_from_file_location("nas_cwa_audit", module_path)
audit = module_from_spec(spec)
spec.loader.exec_module(audit)


def fixtures():
    containers = [
        {"id": "c1", "name": "calibre-web-automated", "image": "crocodilestick/calibre-web-automated:latest", "state": "running"},
        {"id": "r1", "name": "royalbooks", "image": "royalbooks:latest", "state": "running"},
    ]
    mounts = {
        "c1": [
            {"Source": "/volume1/cwa/ingest", "Destination": "/cwa-book-ingest", "RW": True},
            {"Source": "/volume1/books", "Destination": "/calibre-library", "RW": True},
        ],
        "r1": [
            {"Source": "/volume1/cwa/ingest", "Destination": "/cwa-ingest", "RW": True},
            {"Source": "/volume1/books", "Destination": "/cwa-library", "RW": False},
            {"Source": "/volume2/royalbooks", "Destination": "/data", "RW": True},
        ],
    }
    return containers, mounts


def test_matched_mounts_still_do_not_claim_import():
    containers, mounts = fixtures()
    r = audit.inspect_snapshot(containers, mounts)
    assert r["status"] == "mounts_match_not_import_verified"
    assert r["changes_made"] is False
    assert "buchimport" in r["notice"].lower()
    assert all(c["level"] == "ok" for c in r["checks"])


def test_mismatch_detected_without_changes():
    containers, mounts = fixtures()
    mounts["r1"][0]["Source"] = "/volume1/not-cwa-ingest"
    r = audit.inspect_snapshot(containers, mounts)
    assert r["status"] == "blocked"
    assert next(c for c in r["checks"] if c["name"] == "shared_ingest")["level"] == "error"


def test_readwrite_calibre_mount_blocked():
    containers, mounts = fixtures()
    mounts["r1"][1]["RW"] = True
    r = audit.inspect_snapshot(containers, mounts)
    assert r["status"] == "blocked"
    assert next(c for c in r["checks"] if c["name"] == "shared_library")["level"] == "error"


def test_missing_royalbooks_allows_cwa_setup_discovery():
    containers, mounts = fixtures()
    r = audit.inspect_snapshot(containers[:1], mounts)
    assert r["status"] == "needs_setup"
    assert r["mounts"]["cwa_ingest_host"] == "/volume1/cwa/ingest"


def test_missing_cwa_is_blocker():
    containers, mounts = fixtures()
    r = audit.inspect_snapshot(containers[1:], mounts)
    assert r["status"] == "blocked"


def test_not_running_is_blocker():
    containers, mounts = fixtures()
    containers[0]["state"] = "exited"
    r = audit.inspect_snapshot(containers, mounts)
    assert r["status"] == "blocked"


def test_overlap_library_and_ingest_is_blocker():
    containers, mounts = fixtures()
    mounts["c1"][1]["Source"] = "/volume1/cwa/ingest/subfolder"
    r = audit.inspect_snapshot(containers, mounts)
    assert r["status"] == "blocked"


def test_storage_overlap_is_blocker():
    containers, mounts = fixtures()
    mounts["r1"][2]["Source"] = "/volume1/books/royalbooks"
    r = audit.inspect_snapshot(containers, mounts)
    assert r["status"] == "blocked"


def test_ambiguous_containers_require_explicit_id():
    containers, mounts = fixtures()
    containers.append({"id": "c2", "name": "calibre-web-automated-old", "image": "crocodilestick/calibre-web-automated", "state": "exited"})
    r = audit.inspect_snapshot(containers, mounts)
    assert r["status"] == "blocked"
    assert any(c["name"] == "cwa_discovery" for c in r["checks"])
    r = audit.inspect_snapshot(containers, mounts, cwa_name="calibre-web-automated")
    assert r["status"] == "mounts_match_not_import_verified"


def test_no_docker_calls_with_pure_snapshot(monkeypatch):
    containers, mounts = fixtures()
    def forbidden(*a, **k):
        raise AssertionError("Must not touch host")
    monkeypatch.setattr(audit.subprocess, "run", forbidden)
    assert audit.inspect_snapshot(containers, mounts)["status"] == "mounts_match_not_import_verified"


def test_host_reader_only_fetches_mounts(monkeypatch):
    calls = []
    def fake_run(argv, **kwargs):
        calls.append(argv)
        class Result:
            returncode = 0
            stderr = ""
            stdout = ""
        r = Result()
        if argv[1] == "ps":
            r.stdout = "c1\tcalibre-web-automated\tcrocodilestick/calibre-web-automated\trunning\n"
        else:
            r.stdout = '[{"Destination": "/cwa-book-ingest", "Source": "/data/ingest", "RW": true}]'
        return r
    monkeypatch.setattr(audit.subprocess, "run", fake_run)
    containers, mounts = audit.read_snapshot()
    assert containers[0]["id"] == "c1"
    assert mounts["c1"][0]["Destination"] == "/cwa-book-ingest"
    assert all("exec" not in call and ".Config.Env" not in " ".join(call) for call in calls)
