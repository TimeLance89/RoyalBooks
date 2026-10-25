"""Read-only configuration diagnostics for a RoyalBooks + CWA deployment.

No network traffic, directory creation, library writes, or permission changes.
Run inside the RoyalBooks container so checks describe effective container mounts.
"""
from __future__ import annotations

import argparse
import json
import os
import sqlite3
from royalbooks.database import connect as db_connect
import shutil
from pathlib import Path
from typing import Any


def _check(name: str, status: str, detail: str) -> dict[str, str]:
    return {"name": name, "status": status, "detail": detail}


def _directory(path: str, *, writable: bool) -> tuple[Path | None, str]:
    try:
        target = Path(path).resolve(strict=True)
        if not target.is_dir():
            return None, "not_a_directory"
        if not os.access(target, os.R_OK | os.X_OK | (os.W_OK if writable else 0)):
            return target, "permissions_insufficient"
        return target, "ok"
    except (OSError, RuntimeError):
        return None, "not_accessible"


def diagnose(*, data_path: str, ingest_path: str | None = None, library_path: str | None = None,
             api_token: str | None = None) -> dict[str, Any]:
    checks = []
    data, state = _directory(data_path, writable=True)
    checks.append(_check("data_mount", "ok" if state == "ok" else "error",
                         "Persistent writable /data directory" if state == "ok" else state))
    if data:
        try:
            free = shutil.disk_usage(data).free
            checks.append(_check("data_free_space", "ok" if free >= 128 * 1024 * 1024 else "warning",
                                 f"{free // (1024 * 1024)} MiB available"))
        except OSError:
            checks.append(_check("data_free_space", "warning", "unavailable"))

    if not api_token:
        checks.append(_check("queue_authentication", "warning", "No API token: acquisition is disabled"))
    elif len(api_token) < 24:
        checks.append(_check("queue_authentication", "warning", "API token configured but shorter than recommended"))
    else:
        checks.append(_check("queue_authentication", "ok", "API token configured (value hidden)"))

    if ingest_path:
        ingest, state = _directory(ingest_path, writable=True)
        checks.append(_check("cwa_ingest_mount", "ok" if state == "ok" else "error",
                             "Import directory exists and is writable; CWA watcher not independently verified"
                             if state == "ok" else state))
        if data and ingest and (ingest == data or ingest.is_relative_to(data) or data.is_relative_to(ingest)):
            checks.append(_check("cwa_ingest_isolation", "error", "CWA ingest must not overlap RoyalBooks storage"))
        elif ingest:
            checks.append(_check("cwa_ingest_isolation", "ok", "Ingest and data mounts appear separated"))
        if ingest:
            try:
                free = shutil.disk_usage(ingest).free
                checks.append(_check("cwa_ingest_free_space", "ok" if free >= 64 * 1024 * 1024 else "warning",
                                     f"{free // (1024 * 1024)} MiB available"))
            except OSError:
                checks.append(_check("cwa_ingest_free_space", "warning", "unavailable"))
    else:
        ingest = None
        checks.append(_check("cwa_ingest_mount", "warning", "Not configured; files remain staged"))

    if library_path:
        library, state = _directory(library_path, writable=False)
        checks.append(_check("calibre_library_mount", "ok" if state == "ok" else "warning",
                             "Mounted directory readable (Calibre writes not attempted)"
                             if state == "ok" else state))
        if library:
            if data and (library == data or library.is_relative_to(data)):
                checks.append(_check("calibre_library_isolation", "error", "Library must not be inside RoyalBooks data"))
            if ingest and (library == ingest or ingest.is_relative_to(library) or library.is_relative_to(ingest)):
                checks.append(_check("calibre_library_isolation", "error", "Ingest and library folders must be separate"))
            metadata = library / "metadata.db"
            if not metadata.is_file():
                checks.append(_check("calibre_metadata", "warning", "No metadata.db found; inspect the actual CWA bind mount"))
            else:
                try:
                    # Use only read-only SQLite; no metadata.db modifications.
                    with db_connect(metadata.as_uri() + "?mode=ro", uri=True, timeout=1) as db:
                        db.execute("PRAGMA query_only=ON")
                        tables = {row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
                        if {"books", "data"} <= tables:
                            checks.append(_check("calibre_metadata", "ok", "Calibre database is readable (read only)"))
                        else:
                            checks.append(_check("calibre_metadata", "warning", "Calibre books/data tables missing"))
                except (OSError, sqlite3.Error, ValueError):
                    checks.append(_check("calibre_metadata", "warning", "Cannot read metadata.db; WAL/mount may be incompatible"))
    else:
        checks.append(_check("calibre_library_mount", "warning", "Optional read-only library verification disabled"))

    checks.append(_check("cwa_watcher", "warning",
                         "Cannot determine whether CWA is running or watching this same host folder. "
                         "Check its Docker bind mount and CWA logs."))
    checks.append(_check("public_access", "warning",
                         "The search UI lacks login. Restrict port access to a trusted LAN/VPN."))
    return {"status": "error" if any(item["status"] == "error" for item in checks) else "review_required",
            "checks": checks, "changes_made": False}


def main() -> None:
    parser = argparse.ArgumentParser(description="Read-only RoyalBooks NAS/CWA preflight")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args()
    report = diagnose(
        data_path=str(Path(os.getenv("ROYALBOOKS_DB_PATH", "/data/royalbooks.sqlite3")).parent),
        ingest_path=os.getenv("ROYALBOOKS_CWA_INGEST_PATH") or None,
        library_path=os.getenv("ROYALBOOKS_CWA_LIBRARY_PATH") or None,
        api_token=os.getenv("ROYALBOOKS_API_TOKEN") or None,
    )
    if args.json:
        print(json.dumps(report, ensure_ascii=False, indent=2))
    else:
        for row in report["checks"]:
            print(f'{row["status"].upper():7} {row["name"]}: {row["detail"]}')
    if report["status"] == "error":
        raise SystemExit(2)


if __name__ == "__main__":
    main()
