"""Read-only, exact SHA-256 presence checks against an optional Calibre library.

An identical EPUB found in Calibre proves presence, not that this specific
RoyalBooks delivery caused the import. Negative results are inconclusive:
CWA may rewrite EPUB contents or refresh file metadata while ingesting.
"""
from __future__ import annotations

import hashlib
import sqlite3
from royalbooks.database import connect as db_connect
from pathlib import Path, PurePosixPath

MAX_CANDIDATES = 100
MAX_BYTES = 32 * 1024 * 1024
NOTICE = ("Identical EPUB presence does not establish which job imported it. "
          "No exact match does not prove failure; CWA may rewrite EPUB contents.")


def _inside_library(root: Path, relative_dir: str, stem: str) -> Path | None:
    if not isinstance(relative_dir, str) or not isinstance(stem, str):
        return None
    if not relative_dir or not stem or "/" in stem or "\\" in stem or ":" in stem:
        return None
    if stem in {".", ".."}:
        return None
    parts = PurePosixPath(relative_dir)
    if parts.is_absolute() or ".." in parts.parts or "\\" in relative_dir:
        return None
    try:
        file = root.joinpath(*parts.parts, stem + ".epub").resolve(strict=True)
        if not file.is_relative_to(root) or not file.is_file():
            return None
        return file
    except (OSError, RuntimeError):
        return None


def verify_library_presence(job: dict, library_path: str | None) -> dict:
    result = {"status": "not_checked", "notice": NOTICE}
    if not library_path:
        result["status"] = "library_not_configured"
        return result
    if job.get("status") != "delivered":
        result["status"] = "job_not_delivered"
        return result
    digest, size = job.get("sha256"), job.get("file_size")
    if not (isinstance(digest, str) and len(digest) == 64
            and all(c in "0123456789abcdef" for c in digest.lower())
            and type(size) is int and 0 < size <= MAX_BYTES):
        result["status"] = "missing_verified_download_digest"
        return result
    try:
        root = Path(library_path).resolve(strict=True)
        if not root.is_dir():
            raise OSError("not_directory")
    except (OSError, RuntimeError):
        result["status"] = "library_unavailable"
        return result
    metadata = root / "metadata.db"
    if not metadata.is_file():
        result["status"] = "calibre_metadata_missing"
        return result
    try:
        # Read-only SQLite URI. Do not open the live Calibre database for writes.
        with db_connect(metadata.as_uri() + "?mode=ro", uri=True, timeout=3) as db:
            rows = db.execute("""
                SELECT b.id,b.title,b.path,d.name FROM books b
                JOIN data d ON b.id=d.book
                WHERE UPPER(d.format)='EPUB' AND d.uncompressed_size=?
                ORDER BY b.id LIMIT ?
            """, (size, MAX_CANDIDATES + 1)).fetchall()
    except (OSError, sqlite3.Error):
        result["status"] = "calibre_metadata_unreadable"
        return result
    if len(rows) > MAX_CANDIDATES:
        result["status"] = "too_many_candidates_unverified"
        return result
    matching, checked = [], 0
    for book_id, title, relative_dir, stem in rows:
        file = _inside_library(root, relative_dir, stem)
        if file is None:
            continue
        try:
            if file.stat().st_size != size:
                continue
            checked += 1
            checksum = hashlib.sha256()
            with file.open("rb") as stream:
                for block in iter(lambda: stream.read(1024 * 1024), b""):
                    checksum.update(block)
            if checksum.hexdigest() == digest.lower():
                matching.append({"book_id": book_id, "title": str(title)[:150]})
        except (OSError, RuntimeError):
            continue
    result.update(status="identical_epub_present" if matching else "no_exact_match_unverified",
                  checked_files=checked, candidate_records=len(rows), matches=matching[:5])
    return result
