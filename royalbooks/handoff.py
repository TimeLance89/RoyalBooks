"""Read-only Calibre ingest observations, not import verification.

Calibre-Web Automated removes files after processing, including some failures.
A missing file is therefore NEVER labeled as a successful import.
"""
from __future__ import annotations

import re
from pathlib import Path

VALID_ID = re.compile(r"^[a-f0-9]{32}$")


def _exists(path: Path) -> bool:
    try:
        return path.is_file()
    except OSError:
        return False


def observe_handoff(job: dict, staging: Path, ingest: Path | None) -> str | None:
    status, job_id, source_id = job.get("status"), job.get("id"), job.get("source_id")
    if status not in {"staged", "delivering", "needs_review", "delivered"}:
        return None
    if (not isinstance(job_id, str) or not VALID_ID.fullmatch(job_id)
            or not isinstance(source_id, int) or source_id <= 0):
        return "manual_review_unknown"
    staged = staging / (job_id + ".epub")
    if status == "staged":
        return "staged_ready" if _exists(staged) else "staged_missing"
    if ingest is None:
        return "ingest_not_configured"
    try:
        if not ingest.is_dir():
            return "ingest_unavailable"
    except OSError:
        return "ingest_unavailable"
    delivered = ingest / f"gutenberg-{source_id}-{job_id}.epub"
    if status == "delivered":
        return "awaiting_ingest" if _exists(delivered) else "file_absent_unverified"
    if _exists(delivered):
        return "manual_review_destination_present"
    if _exists(staged):
        return "manual_review_staged_present"
    return "manual_review_unknown"
