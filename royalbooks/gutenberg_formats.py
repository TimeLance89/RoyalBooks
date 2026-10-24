"""First-party Gutenberg RDF format index, imported from the official bulk feed.

No per-book site crawling, guessed URLs or user-supplied download addresses.
The RDF feed describes offered formats; only a successful EPUB transfer+validation
proves that a link works. German rights are never inferred from RDF metadata.
"""
from __future__ import annotations

import argparse
import asyncio
import hashlib
import os
import re
import sqlite3
import tarfile
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit

import httpx
from defusedxml import ElementTree as ET

from royalbooks.gutenberg_catalog import connect

RDF_URL = "https://www.gutenberg.org/cache/epub/feeds/rdf-files.tar.bz2"
MAX_ARCHIVE = 256 * 1024 * 1024
MAX_RECORD = 1024 * 1024
MAX_TOTAL = 3 * 1024 * 1024 * 1024
MAX_MEMBERS = 240000
RDF = "{http://www.w3.org/1999/02/22-rdf-syntax-ns#}"
DCT = "{http://purl.org/dc/terms/}"
PG = "{http://www.gutenberg.org/2009/pgterms/}"
SCHEMA = """
CREATE TABLE IF NOT EXISTS gutenberg_formats (
    ebook_id INTEGER PRIMARY KEY,
    epub_url TEXT NOT NULL,
    extent INTEGER
);
CREATE TABLE IF NOT EXISTS gutenberg_formats_meta (
    singleton INTEGER PRIMARY KEY CHECK(singleton=1),
    records INTEGER NOT NULL, archive_sha256 TEXT NOT NULL, updated_at TEXT NOT NULL
);
"""


def safe_epub_uri(url: str, ebook_id: int) -> str | None:
    """Permit only absolute first-party ebook-specific URLs found in trusted RDF."""
    try:
        parts = urlsplit(url)
        if (parts.scheme not in {"http", "https"} or parts.hostname not in {"gutenberg.org", "www.gutenberg.org"}
                or parts.username or parts.password or parts.port not in (None, 80, 443)
                or parts.query or parts.fragment):
            return None
        path = parts.path
        if not (path.startswith(f"/ebooks/{ebook_id}.") or path.startswith(f"/cache/epub/{ebook_id}/pg{ebook_id}")):
            return None
        if not (".epub" in path or ".epub3" in path):
            return None
        return urlunsplit(("https", "www.gutenberg.org", path, "", ""))
    except ValueError:
        return None


def parse_rdf_entry(content: bytes, ebook_id: int) -> tuple[int, str, int | None] | None:
    if len(content) > MAX_RECORD:
        raise ValueError("rdf_entry_too_large")
    root = ET.fromstring(content)
    books = root.findall(f".//{PG}ebook")
    if not books:
        return None
    candidates = []
    for book in books:
        about = book.get(f"{RDF}about", "")
        if about.rsplit("/", 1)[-1] != str(ebook_id):
            continue
        for ref in book.findall(f"{DCT}hasFormat"):
            for entry in ref.findall(f"{PG}file"):
                addr = entry.get(f"{RDF}about", "")
                url = safe_epub_uri(addr, ebook_id)
                if url is None:
                    continue
                values = [v.text.strip().lower() for v in entry.findall(f".//{RDF}value") if v.text]
                if not any(v.startswith("application/epub+zip") for v in values):
                    continue
                extent = entry.find(f"{DCT}extent")
                try:
                    size = int(extent.text) if extent is not None and extent.text else None
                except ValueError:
                    size = None
                # Prefer small no-image EPUBs over large images, but store all
                # metadata conservatively; validation occurs after download.
                priority = 0 if url.endswith(".epub") else 1 if "noimages" in url else 2
                candidates.append((priority, url, size))
    if not candidates:
        return None
    _, url, size = min(candidates, key=lambda row: (row[0], row[1]))
    return ebook_id, url, size


def import_rdf_archive(filename: str | Path, db_path: str | Path) -> dict:
    path = Path(filename)
    if not path.is_file() or path.stat().st_size > MAX_ARCHIVE:
        raise ValueError("rdf_archive_missing_or_too_large")
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    Path(db_path).parent.mkdir(parents=True, exist_ok=True)
    db = connect(str(db_path))
    try:
        db.executescript(SCHEMA)
        db.execute("BEGIN IMMEDIATE")
        db.execute("DELETE FROM gutenberg_formats")
        total = count = matched = 0
        # Streaming mode: archive members are read one by one; never extract.
        with tarfile.open(path, mode="r|bz2") as archive:
            for member in archive:
                count += 1
                if count > MAX_MEMBERS:
                    raise ValueError("rdf_archive_too_many_members")
                if not member.isfile():
                    continue
                if member.size < 0 or member.size > MAX_RECORD:
                    raise ValueError("rdf_member_size_invalid")
                total += member.size
                if total > MAX_TOTAL:
                    raise ValueError("rdf_uncompressed_limit")
                match = re.search(r"(?:^|/)pg([0-9]{1,8})\.rdf$", member.name)
                if not match:
                    continue
                ebook_id = int(match.group(1))
                if ebook_id <= 0:
                    continue
                file = archive.extractfile(member)
                if file is None:
                    raise ValueError("rdf_archive_member_unreadable")
                with file:
                    content = file.read(MAX_RECORD + 1)
                data = parse_rdf_entry(content, ebook_id)
                if data:
                    db.execute("INSERT INTO gutenberg_formats(ebook_id,epub_url,extent) VALUES (?,?,?)", data)
                    matched += 1
        if matched == 0:
            raise ValueError("rdf_archive_no_epub_records")
        updated_at = datetime.now(timezone.utc).isoformat()
        db.execute("""INSERT INTO gutenberg_formats_meta(singleton,records,archive_sha256,updated_at)
           VALUES (1,?,?,?) ON CONFLICT(singleton) DO UPDATE SET
           records=excluded.records,archive_sha256=excluded.archive_sha256,
           updated_at=excluded.updated_at""", (matched, digest.hexdigest(), updated_at))
        db.commit()
        return {"records": matched, "updated_at": updated_at, "members": count}
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()


def format_status(db_path: str | Path) -> dict:
    if not Path(db_path).is_file():
        return {"indexed": False, "records": 0, "updated_at": None}
    try:
        with connect(str(db_path)) as db:
            row = db.execute("SELECT records,updated_at FROM gutenberg_formats_meta WHERE singleton=1").fetchone()
        return {"indexed": row is not None, "records": row["records"] if row else 0,
                "updated_at": row["updated_at"] if row else None}
    except sqlite3.Error:
        return {"indexed": False, "records": 0, "updated_at": None}


def lookup_epub(db_path: str, ebook_id: int) -> str | None:
    if not format_status(db_path)["indexed"]:
        return None
    with connect(db_path) as db:
        row = db.execute("SELECT epub_url FROM gutenberg_formats WHERE ebook_id=?", (ebook_id,)).fetchone()
    return row["epub_url"] if row and safe_epub_uri(row["epub_url"], ebook_id) else None


def lookup_epubs(db_path: str, ebook_ids: list[int]) -> dict[int, str]:
    if not ebook_ids or not format_status(db_path)["indexed"]:
        return {}
    unique = sorted(set(ebook_ids))[:50]
    marks = ",".join("?" for _ in unique)
    with connect(db_path) as db:
        rows = db.execute(f"SELECT ebook_id, epub_url FROM gutenberg_formats WHERE ebook_id IN ({marks})",
                          unique).fetchall()
    return {row["ebook_id"]: row["epub_url"] for row in rows
            if safe_epub_uri(row["epub_url"], row["ebook_id"])}


async def download_archive(dest: Path):
    dest.parent.mkdir(parents=True, exist_ok=True)
    async with httpx.AsyncClient(timeout=httpx.Timeout(60.0), follow_redirects=False,
                                 headers={"User-Agent": "RoyalBooks/0.5 (https://github.com/TimeLance89/RoyalBooks)"}) as client:
        async with client.stream("GET", RDF_URL) as response:
            response.raise_for_status()
            if response.status_code != 200:
                raise ValueError("rdf_download_redirect_or_error")
            length = response.headers.get("content-length", "")
            if length.isdigit() and int(length) > MAX_ARCHIVE:
                raise ValueError("rdf_archive_too_large")
            count = 0
            with dest.open("xb") as out:
                async for part in response.aiter_bytes(131072):
                    count += len(part)
                    if count > MAX_ARCHIVE:
                        raise ValueError("rdf_archive_too_large")
                    out.write(part)


def main():
    parser = argparse.ArgumentParser(description="One-time official bulk RDF/EPUB format metadata import")
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--file", type=Path, help="Local official rdf-files.tar.bz2")
    group.add_argument("--download", action="store_true", help="Explicit one-time transfer of official bulk archive")
    parser.add_argument("--db", default=os.getenv("ROYALBOOKS_DB_PATH", "/data/royalbooks.sqlite3"))
    args = parser.parse_args()
    if args.download:
        with tempfile.TemporaryDirectory(prefix="royalbooks-rdf-") as folder:
            archive = Path(folder) / "rdf-files.tar.bz2"
            asyncio.run(download_archive(archive))
            summary = import_rdf_archive(archive, args.db)
    else:
        summary = import_rdf_archive(args.file, args.db)
    print(f"Indexed {summary['records']} first-party Gutenberg EPUB metadata links.")
    print("Links are not verified downloads. Verify local rights before acquiring any ebook.")


if __name__ == "__main__":
    main()
