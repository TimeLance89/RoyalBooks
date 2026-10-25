"""Restart-safe, opt-in Gutenberg EPUB ingest; never modifies Calibre metadata.db."""
from __future__ import annotations

import asyncio
import hashlib
import os
import secrets
import shutil
import sqlite3
from royalbooks.database import connect as db_connect
import uuid
import zipfile
from time import monotonic
from pathlib import Path, PurePosixPath
from urllib.parse import urljoin, urlsplit, parse_qs
from defusedxml import ElementTree

import httpx

from royalbooks.providers import Gutendex, ProviderError
from royalbooks.direct_sources import SOURCES, lookup_epub as lookup_direct_epub, safe_epub_url
from royalbooks.libgen_links import resolve_epub, safe_file_url

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
            if len(filenames) != len(entries) or archive.testzip() is not None:
                raise AcquisitionError('invalid_epub_crc')
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
            if "v2_authorized" not in columns:
                # Existing queued jobs remain preserved but paused on upgrade.
                db.execute("ALTER TABLE jobs ADD COLUMN v2_authorized INTEGER NOT NULL DEFAULT 0")
            # Download attempts are counted on claim, including interrupted attempts.
            db.execute("""UPDATE jobs SET status='failed', error='interrupted_attempt_limit',
                        updated_at=CURRENT_TIMESTAMP
                        WHERE status='downloading' AND attempts >= ?""", (MAX_ATTEMPTS,))
            db.execute("""UPDATE jobs SET status='queued', error=NULL,
                        updated_at=CURRENT_TIMESTAMP
                        WHERE status='downloading' AND attempts < ?""", (MAX_ATTEMPTS,))
            db.execute("""UPDATE jobs SET status='needs_review',
                        error='Interrupted during ingest handoff',
                        updated_at=CURRENT_TIMESTAMP WHERE status='delivering'""")

    def _db(self):
        db = db_connect(self.path, timeout=10)
        db.row_factory = sqlite3.Row
        return db

    def add(self, source_id: int, source: str = "gutendex", *, v2_authorized: bool = False) -> dict:
        if source not in {"gutendex", "gutenberg_catalog", *SOURCES}:
            raise JobStateError("unsupported_source")
        with self._db() as db:
            db.execute("INSERT OR IGNORE INTO jobs(id,source,source_id,status,v2_authorized) VALUES (?,?,?,'queued',?)",
                       (uuid.uuid4().hex, source, source_id, int(v2_authorized)))
            if v2_authorized:
                db.execute("UPDATE jobs SET v2_authorized=1 WHERE source=? AND source_id=? AND status='queued'",
                           (source, source_id))
            row = db.execute("SELECT * FROM jobs WHERE source=? AND source_id=?", (source, source_id)).fetchone()
            return dict(row)

    def list(self) -> list[dict]:
        with self._db() as db:
            return [dict(x) for x in db.execute("SELECT * FROM jobs ORDER BY created_at DESC LIMIT 100")]

    def claim(self, statuses: tuple[str, ...], source: str | None = None, *, v2_only: bool = False) -> dict | None:
        with self._db() as db:
            db.execute("BEGIN IMMEDIATE")
            for status in statuses:
                if status == "queued":
                    # Guard against old databases containing already-exhausted queued jobs.
                    db.execute("""UPDATE jobs SET status='failed', error='interrupted_attempt_limit',
                                updated_at=CURRENT_TIMESTAMP
                                WHERE status='queued' AND attempts >= ?""", (MAX_ATTEMPTS,))
                sql = 'SELECT * FROM jobs WHERE status=?'
                args = [status]
                if source is not None:
                    sql += ' AND source=?'
                    args.append(source)
                if v2_only:
                    sql += ' AND v2_authorized=1'
                row = db.execute(sql + ' ORDER BY created_at LIMIT 1', args).fetchone()
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

    def retry_failed(self, job_id: str, *, v2_authorized: bool = False) -> dict:
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
                        v2_authorized=CASE WHEN ? THEN 1 ELSE v2_authorized END,
                        file_size=NULL,updated_at=CURRENT_TIMESTAMP
                        WHERE id=? AND status='failed'""", (int(v2_authorized), job_id))
            return dict(db.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone())


async def download_epub(client: httpx.AsyncClient, url: str, destination: Path, source: str = "gutendex") -> int:
    headers = {}
    checker = valid_url if source not in SOURCES else lambda u: safe_epub_url(source, u)
    if source == 'libgen':
        wanted_md5 = [value.lower() for value in parse_qs(urlsplit(url).query).get('md5', [])]
        headers['Referer'] = f'https://{urlsplit(url).hostname}/ads.php?md5={wanted_md5[0]}' if wanted_md5 else ''
        checker = lambda candidate: (safe_file_url(candidate) and
            [value.lower() for value in parse_qs(urlsplit(candidate).query).get('md5', [])] == wanted_md5)
    if not checker(url):
        raise AcquisitionError("untrusted_download_host")
    for _ in range(4):
        async with client.stream("GET", url, follow_redirects=False, headers=headers) as response:
            if response.status_code in (301, 302, 303, 307, 308):
                target = urljoin(url, response.headers.get("location", ""))
                if not checker(target):
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


async def process_queued(job: dict, store: JobStore, staging: Path, client: httpx.AsyncClient,
                         provider: Gutendex | None = None) -> None:
    temporary = staging / (job['id'] + '.part')
    target = staging / (job['id'] + '.epub')
    try:
        if job["source"] == "gutenberg_catalog":
            # No Gutendex calls: the local, first-party RDF index supplies the URI.
            # This index grants no copyright permission; the API requires user confirmation.
            from royalbooks.gutenberg_formats import lookup_epub
            url = await asyncio.to_thread(lookup_epub, store.path, job["source_id"])
            if not url:
                raise AcquisitionError("official_epub_metadata_missing")
        elif job["source"] in SOURCES:
            url = await asyncio.to_thread(lookup_direct_epub, store.path, job["source"], job["source_id"])
            if not url:
                raise AcquisitionError("publisher_epub_metadata_missing_or_expired")
            if job['source'] == 'libgen':
                expected_md5 = parse_qs(urlsplit(url).query)['md5'][0].lower()
                url = await resolve_epub(client, url)
        elif job["source"] == "gutendex":
            catalog = provider if provider is not None else Gutendex()
            item = await catalog.get_json(client, f"https://gutendex.com/books/{job['source_id']}/", {})
            if item.get("id") != job['source_id'] or item.get('copyright') is not False:
                raise AcquisitionError('source_not_eligible')
            formats = item.get('formats')
            url = formats.get('application/epub+zip') if isinstance(formats, dict) else None
            if not isinstance(url, str) or not valid_url(url):
                raise AcquisitionError('no_trusted_epub')
        else:
            raise AcquisitionError("unsupported_source")
        size = await download_epub(client, url, temporary, source=job["source"])
        digest = validate_epub(temporary)
        if job['source'] == 'libgen':
            with temporary.open('rb') as downloaded:
                actual_md5 = hashlib.file_digest(downloaded, 'md5').hexdigest()
            if actual_md5 != expected_md5:
                raise AcquisitionError('catalog_md5_mismatch')
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
        # Never invent an ingest folder: Docker/CWA may be watching a different mount.
        # A missing directory means the handoff is uncertain and needs manual review.
        if not ingest.is_dir():
            raise AcquisitionError("ingest_directory_missing")
        if ingest.is_symlink():
            raise AcquisitionError("ingest_symlink_forbidden")
        # If the directory was misconfigured to overlap our staging, refuse.
        if ingest.resolve() == staging.resolve() or ingest.resolve().is_relative_to(staging.resolve()):
            raise AcquisitionError("ingest_overlaps_staging")
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


async def worker(store: JobStore, data_dir: Path, ingest: Path | None,
                 gutendex: Gutendex | None = None, *, include_legacy: bool = True) -> None:
    staging = data_dir / 'staging'
    staging.mkdir(parents=True, exist_ok=True)
    catalog = gutendex if gutendex is not None else Gutendex()
    libgen_blocked_until = 0.0
    async with httpx.AsyncClient(timeout=httpx.Timeout(20.0), follow_redirects=False, trust_env=False,
                                 headers={'User-Agent': 'RoyalBooks/2.0 (https://github.com/TimeLance89/RoyalBooks)'}) as client:
        while True:
            if ingest is not None:
                job = await asyncio.to_thread(store.claim, ('staged',), v2_only=not include_legacy)
                if job:
                    await asyncio.to_thread(deliver_staged, job, store, staging, ingest)
                    continue
            # A 403/451 cooldown must not consume another persisted job attempt.
            if catalog.cooldown_remaining() or libgen_blocked_until > monotonic():
                # Gutendex refusal must not stop independent first-party RDF jobs.
                job = None
                candidates = ['gutenberg_catalog', *sorted(SOURCES)]
                if not catalog.cooldown_remaining():
                    candidates.append('gutendex')
                for independent in candidates:
                    if independent == 'libgen' and libgen_blocked_until > monotonic():
                        continue
                    job = await asyncio.to_thread(store.claim, ('queued',), independent, v2_only=not include_legacy)
                    if job:
                        break
            else:
                job = await asyncio.to_thread(store.claim, ('queued',), v2_only=not include_legacy)
            if job:
                try:
                    await asyncio.wait_for(process_queued(job, store, staging, client, catalog), timeout=180)
                except TimeoutError:
                    await asyncio.to_thread(store.update, job['id'], 'failed', error='ReadTimeout')
                if job['source'] == 'libgen':
                    actual = await asyncio.to_thread(store.get, job['id'])
                    if actual and actual['error'] in {'http_403', 'http_451'}:
                        libgen_blocked_until = monotonic() + 1800
                continue
            await asyncio.sleep(1.0)


def require_token(configured: str | None, provided: str | None) -> None:
    if not configured:
        raise PermissionError('acquisition_disabled_without_token')
    if not provided or not secrets.compare_digest(configured, provided):
        raise PermissionError('invalid_api_token')
