"""Restart-safe, opt-in Gutenberg EPUB ingest; never modifies Calibre metadata.db."""
from __future__ import annotations

import asyncio
import hashlib
import os
import secrets
import shutil
import sqlite3
import uuid
import zipfile
from pathlib import Path, PurePosixPath
from urllib.parse import urljoin, urlsplit
from defusedxml import ElementTree

import httpx

from royalbooks.providers import Gutendex, ProviderError

MAX_BYTES = 32 * 1024 * 1024
MAX_UNCOMPRESSED = 160 * 1024 * 1024
TRUSTED_HOSTS = {"gutenberg.org", "www.gutenberg.org", "gutenberg.pglaf.org"}
MAX_ATTEMPTS = 3
RETRYABLE_ERRORS = {
    "ReadTimeout", "ConnectTimeout", "ConnectError", "ReadError",
    "RemoteProtocolError", "PoolTimeout", "WriteTimeout", "WriteError",
    "http_429", "http_500", "http_502", "http_503", "http_504",
}


class JobStateError(Exception):
    """Invalid or unsafe requested job state transition."""



class AcquisitionError(Exception):
    pass


def valid_url(url: str) -> bool:
    try:
        parsed = urlsplit(url)
        return (parsed.scheme == "https" and parsed.hostname in TRUSTED_HOSTS
                and parsed.port in (None, 443) and not parsed.username and not parsed.password
                and parsed.path.startswith(("/ebooks/", "/cache/epub/", "/files/")))
    except ValueError:
        return False


def validate_epub(path: Path) -> str:
    """Check ZIP limits and EPUB package structure without unpacking content."""
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    try:
        with zipfile.ZipFile(path) as archive:
            entries = archive.infolist()
            if len(entries) > 2500 or not entries or entries[0].filename != "mimetype":
                raise AcquisitionError("invalid_epub_package")
            if (entries[0].compress_type != zipfile.ZIP_STORED or entries[0].file_size != 20
                    or archive.read("mimetype") != b"application/epub+zip"):
                raise AcquisitionError("invalid_epub_mimetype")
            total = 0
            filenames = set()
            for entry in entries:
                parts = PurePosixPath(entry.filename).parts
                if not parts or entry.filename.startswith("/") or ".." in parts or "\\" in entry.filename:
                    raise AcquisitionError("invalid_archive_path")
                if entry.flag_bits & 0x1:
                    raise AcquisitionError("encrypted_archive")
                if (entry.external_attr >> 16) & 0o170000 == 0o120000:
                    raise AcquisitionError("archive_symlink")
                total += entry.file_size
                if total > MAX_UNCOMPRESSED or (entry.file_size > max(1, entry.compress_size) * 250):
                    raise AcquisitionError("oversized_archive")
                filenames.add(entry.filename)
            if "META-INF/container.xml" not in filenames or archive.getinfo("META-INF/container.xml").file_size > 1024 * 1024:
                raise AcquisitionError("missing_epub_manifest")
            root = ElementTree.fromstring(archive.read("META-INF/container.xml"))
            refs = root.findall(".//{*}rootfile")
            if not any(ref.attrib.get("full-path") in filenames and ref.attrib.get("media-type") == "application/oebps-package+xml"
                       for ref in refs):
                raise AcquisitionError("invalid_epub_manifest")
    except (OSError, zipfile.BadZipFile, ElementTree.ParseError, RuntimeError, KeyError) as exc:
        raise AcquisitionError("invalid_epub_archive") from exc
    return digest.hexdigest()


class JobStore:
    def __init__(self, path: str):
        self.path = path
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        with self._db() as db:
            db.execute("""CREATE TABLE IF NOT EXISTS jobs (
                id TEXT PRIMARY KEY, source TEXT NOT NULL, source_id INTEGER NOT NULL,
                status TEXT NOT NULL, error TEXT, sha256 TEXT, file_size INTEGER,
                attempts INTEGER NOT NULL DEFAULT 0,
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                UNIQUE(source, source_id))""")
            columns = {row[1] for row in db.execute("PRAGMA table_info(jobs)")}
            if "attempts" not in columns:
                db.execute("ALTER TABLE jobs ADD COLUMN attempts INTEGER NOT NULL DEFAULT 0")
            db.execute("UPDATE jobs SET status='queued' WHERE status='downloading'")
            db.execute("UPDATE jobs SET status='needs_review', error='Interrupted during ingest handoff' WHERE status='delivering'")

    def _db(self):
        db = sqlite3.connect(self.path, timeout=10)
        db.row_factory = sqlite3.Row
        return db

    def add(self, source_id: int) -> dict:
        with self._db() as db:
            db.execute("INSERT OR IGNORE INTO jobs(id,source,source_id,status) VALUES (?,?,?,'queued')",
                       (uuid.uuid4().hex, "gutendex", source_id))
            row = db.execute("SELECT * FROM jobs WHERE source='gutendex' AND source_id=?", (source_id,)).fetchone()
            return dict(row)

    def list(self) -> list[dict]:
        with self._db() as db:
            return [dict(x) for x in db.execute("SELECT * FROM jobs ORDER BY created_at DESC LIMIT 100")]

    def claim(self, statuses: tuple[str, ...]) -> dict | None:
        with self._db() as db:
            db.execute("BEGIN IMMEDIATE")
            for status in statuses:
                row = db.execute("SELECT * FROM jobs WHERE status=? ORDER BY created_at LIMIT 1", (status,)).fetchone()
                if row:
                    next_status = 'downloading' if status == 'queued' else 'delivering'
                    db.execute("""UPDATE jobs
                        SET status=?, attempts=attempts+CASE WHEN ?='queued' THEN 1 ELSE 0 END,
                        updated_at=CURRENT_TIMESTAMP WHERE id=? AND status=?""",
                        (next_status, status, row['id'], status))
                    return dict(row)
        return None

    def update(self, job_id: str, status: str, *, error: str | None = None,
               sha256: str | None = None, file_size: int | None = None) -> None:
        with self._db() as db:
            db.execute("""UPDATE jobs SET status=?,error=?,sha256=COALESCE(?,sha256),
                       file_size=COALESCE(?,file_size),updated_at=CURRENT_TIMESTAMP WHERE id=?""",
                       (status, error, sha256, file_size, job_id))

    def sha_exists(self, digest: str, current: str) -> bool:
        with self._db() as db:
            return db.execute("""SELECT 1 FROM jobs WHERE sha256=? AND id!=?
                 AND status IN ('staged','delivering','delivered','needs_review')""",
                              (digest, current)).fetchone() is not None

    def get(self, job_id: str) -> dict | None:
        with self._db() as db:
            row = db.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone()
            return dict(row) if row else None

    def retry_failed(self, job_id: str) -> dict:
        """Limited, atomic retry for transient failures; no ambiguous handoff repeats."""
        with self._db() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone()
            if row is None:
                raise JobStateError("job_not_found")
            if row["status"] != "failed":
                raise JobStateError("retry_requires_failed_status")
            if row["error"] not in RETRYABLE_ERRORS:
                raise JobStateError("error_not_retryable")
            if row["attempts"] >= MAX_ATTEMPTS:
                raise JobStateError("retry_limit_reached")
            db.execute("""UPDATE jobs SET status='queued',error=NULL,sha256=NULL,
                        file_size=NULL,updated_at=CURRENT_TIMESTAMP
                        WHERE id=? AND status='failed'""", (job_id,))
            return dict(db.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone())


async def download_epub(client: httpx.AsyncClient, url: str, destination: Path) -> int:
    if not valid_url(url):
        raise AcquisitionError("untrusted_download_host")
    for _ in range(4):
        async with client.stream("GET", url, follow_redirects=False) as response:
            if response.status_code in (301, 302, 303, 307, 308):
                target = urljoin(url, response.headers.get("location", ""))
                if not valid_url(target):
                    raise AcquisitionError("untrusted_redirect")
                url = target
                continue
            response.raise_for_status()
            length = response.headers.get("content-length")
            if length and int(length) > MAX_BYTES:
                raise AcquisitionError("file_too_large")
            count = 0
            with destination.open("wb") as out:
                async for chunk in response.aiter_bytes(65536):
                    count += len(chunk)
                    if count > MAX_BYTES:
                        raise AcquisitionError("file_too_large")
                    out.write(chunk)
            return count
    raise AcquisitionError("too_many_redirects")


async def process_queued(job: dict, store: JobStore, staging: Path, client: httpx.AsyncClient) -> None:
    temporary = staging / (job['id'] + '.part')
    target = staging / (job['id'] + '.epub')
    try:
        item = await Gutendex().get_json(client, f"https://gutendex.com/books/{job['source_id']}", {})
        if item.get("id") != job['source_id'] or item.get('copyright') is not False:
            raise AcquisitionError('source_not_eligible')
        formats = item.get('formats')
        url = formats.get('application/epub+zip') if isinstance(formats, dict) else None
        if not isinstance(url, str) or not valid_url(url):
            raise AcquisitionError('no_trusted_epub')
        size = await download_epub(client, url, temporary)
        digest = validate_epub(temporary)
        if store.sha_exists(digest, job['id']):
            raise AcquisitionError('duplicate_epub')
        os.replace(temporary, target)
        store.update(job['id'], 'staged', sha256=digest, file_size=size)
    except Exception as exc:
        if isinstance(exc, (AcquisitionError, ProviderError)):
            code = str(exc)
        elif isinstance(exc, httpx.HTTPStatusError):
            code = f"http_{exc.response.status_code}"
        else:
            code = type(exc).__name__
        store.update(job['id'], 'failed', error=code[:120])
    finally:
        temporary.unlink(missing_ok=True)


def deliver_staged(job: dict, store: JobStore, staging: Path, ingest: Path) -> None:
    """Atomic destination rename, then mark handed off. CWA confirms import separately."""
    source = staging / (job['id'] + '.epub')
    dest = ingest / (f"gutenberg-{job['source_id']}-{job['id']}.epub")
    temporary = ingest / (f".{job['id']}.part")
    try:
        if not source.is_file():
            raise AcquisitionError('staged_file_missing')
        if not job.get("sha256") or validate_epub(source) != job["sha256"]:
            raise AcquisitionError("staged_file_hash_mismatch")
        if dest.exists():
            raise AcquisitionError("ingest_filename_already_exists")
        ingest.mkdir(parents=True, exist_ok=True)
        with source.open('rb') as input_file, temporary.open('xb') as output_file:
            shutil.copyfileobj(input_file, output_file)
            output_file.flush()
            os.fsync(output_file.fileno())
        os.replace(temporary, dest)
        store.update(job['id'], 'delivered')
        source.unlink(missing_ok=True)
    except Exception as exc:
        # A crash during delivery goes to needs_review on restart; never auto-repeat blindly.
        store.update(job['id'], 'needs_review', error=(str(exc) if isinstance(exc, AcquisitionError) else type(exc).__name__)[:120])
    finally:
        temporary.unlink(missing_ok=True)


async def worker(store: JobStore, data_dir: Path, ingest: Path | None) -> None:
    staging = data_dir / 'staging'
    staging.mkdir(parents=True, exist_ok=True)
    async with httpx.AsyncClient(timeout=httpx.Timeout(20.0), follow_redirects=False,
                                 headers={'User-Agent': 'RoyalBooks/0.3 (https://github.com/TimeLance89/RoyalBooks)'}) as client:
        while True:
            if ingest is not None:
                job = await asyncio.to_thread(store.claim, ('staged',))
                if job:
                    await asyncio.to_thread(deliver_staged, job, store, staging, ingest)
                    continue
            job = await asyncio.to_thread(store.claim, ('queued',))
            if job:
                await process_queued(job, store, staging, client)
                continue
            await asyncio.sleep(1.0)


def require_token(configured: str | None, provided: str | None) -> None:
    if not configured:
        raise PermissionError('acquisition_disabled_without_token')
    if not provided or not secrets.compare_digest(configured, provided):
        raise PermissionError('invalid_api_token')
