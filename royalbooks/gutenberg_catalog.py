"""Offline Gutenberg search based on its official weekly CSV catalog.

No scraping, bypassing blocks, download guesses or implicit network calls.
The CSV does not contain verified EPUB links or rights for Germany.
"""
from __future__ import annotations

import argparse
import asyncio
import csv
import gzip
import hashlib
import io
import os
import re
import sqlite3
import tempfile
from datetime import datetime, timezone
from pathlib import Path

import httpx

from royalbooks.matching import normalize
from royalbooks.providers import Book, Provider

CATALOG_URL = "https://www.gutenberg.org/cache/epub/feeds/pg_catalog.csv.gz"
MAX_ARCHIVE_BYTES = 12 * 1024 * 1024
MAX_CSV_CHARS = 48 * 1024 * 1024
MAX_BOOKS = 120000
REQUIRED = {"Text#", "Type", "Issued", "Title", "Language", "Authors"}
SCHEMA = """
CREATE TABLE IF NOT EXISTS gutenberg_catalog (
    ebook_id INTEGER PRIMARY KEY, title TEXT NOT NULL, authors TEXT NOT NULL,
    language TEXT NOT NULL, issued TEXT, title_search TEXT NOT NULL, author_search TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_gutenberg_language ON gutenberg_catalog(language);
CREATE TABLE IF NOT EXISTS gutenberg_catalog_meta (
    singleton INTEGER PRIMARY KEY CHECK (singleton=1),
    updated_at TEXT NOT NULL, records INTEGER NOT NULL, archive_sha256 TEXT NOT NULL
);
"""


def connect(path: str) -> sqlite3.Connection:
    db = sqlite3.connect(path, timeout=20)
    db.row_factory = sqlite3.Row
    db.execute("PRAGMA busy_timeout=20000")
    return db


def parse_entries(stream: io.TextIOBase):
    # Respect line breaks inside quoted CSV fields; avoid unbounded allocation.
    csv.field_size_limit(20000)
    reader = csv.DictReader(stream)
    if not reader.fieldnames or not REQUIRED.issubset(set(reader.fieldnames)):
        raise ValueError("invalid_gutenberg_columns")
    total_chars, count = 0, 0
    for row in reader:
        if None in row:
            raise ValueError("corrupted_gutenberg_csv")
        total_chars += sum(len(str(v)) for v in row.values() if v is not None)
        if total_chars > MAX_CSV_CHARS:
            raise ValueError("gutenberg_csv_too_large")
        count += 1
        if count > MAX_BOOKS:
            raise ValueError("gutenberg_csv_too_many_records")
        if row["Type"] != "Text":
            continue
        identifier = (row["Text#"] or "").strip()
        title = (row["Title"] or "").strip()
        authors = (row["Authors"] or "").strip()
        languages = (row["Language"] or "").strip()
        if not (identifier.isascii() and identifier.isdigit() and 0 < int(identifier) <= 10_000_000
                and 2 <= len(title) <= 1000 and len(authors) <= 1000):
            continue
        lang_codes = [code.strip().lower() for code in re.split(r"[;, ]+", languages) if code.strip()]
        lang_codes = [code for code in lang_codes if re.fullmatch(r"[a-z]{2,3}", code)]
        if not lang_codes:
            continue
        issued = (row["Issued"] or "").strip()[:20]
        yield (
            int(identifier), title, authors, ";" + ";".join(lang_codes) + ";", issued,
            normalize(title), normalize(authors)
        )


def import_catalog(path: str | Path, db_path: str | Path) -> int:
    """Replace index in one transaction; failed updates leave old index intact."""
    path = Path(path)
    if path.stat().st_size > MAX_ARCHIVE_BYTES and path.suffix == ".gz":
        raise ValueError("gutenberg_archive_too_large")
    if path.stat().st_size > MAX_CSV_CHARS and path.suffix != ".gz":
        raise ValueError("gutenberg_csv_too_large")
    digest = hashlib.sha256()
    with path.open("rb") as raw:
        for piece in iter(lambda: raw.read(1024 * 1024), b""):
            digest.update(piece)
    Path(db_path).parent.mkdir(parents=True, exist_ok=True)
    database = connect(str(db_path))
    try:
        # The table is created before the explicit transaction. The old rows
        # aren't destroyed unless the entire replacement is validated.
        database.executescript(SCHEMA)
        database.execute("BEGIN IMMEDIATE")
        database.execute("DELETE FROM gutenberg_catalog")
        count = 0
        opener = gzip.open if path.suffix == ".gz" else open
        with opener(path, "rt", encoding="utf-8-sig", errors="strict", newline="") as stream:
            batch = []
            for item in parse_entries(stream):
                batch.append(item)
                count += 1
                if len(batch) >= 1000:
                    database.executemany("""INSERT INTO gutenberg_catalog
                        (ebook_id,title,authors,language,issued,title_search,author_search)
                        VALUES (?,?,?,?,?,?,?)""", batch)
                    batch.clear()
            if batch:
                database.executemany("""INSERT INTO gutenberg_catalog
                    (ebook_id,title,authors,language,issued,title_search,author_search)
                    VALUES (?,?,?,?,?,?,?)""", batch)
        if not count:
            raise ValueError("gutenberg_catalog_empty")
        database.execute("""INSERT INTO gutenberg_catalog_meta(singleton,updated_at,records,archive_sha256)
            VALUES (1,?,?,?) ON CONFLICT(singleton) DO UPDATE SET
            updated_at=excluded.updated_at,records=excluded.records,archive_sha256=excluded.archive_sha256""",
            (datetime.now(timezone.utc).isoformat(), count, digest.hexdigest()))
        database.commit()
        return count
    except Exception:
        database.rollback()
        raise
    finally:
        database.close()


def catalog_status(db_path: str | Path) -> dict:
    if not Path(db_path).is_file():
        return {"indexed": False, "records": 0, "updated_at": None}
    try:
        with connect(str(db_path)) as db:
            row = db.execute("SELECT records,updated_at FROM gutenberg_catalog_meta WHERE singleton=1").fetchone()
        return {"indexed": bool(row), "records": row["records"] if row else 0,
                "updated_at": row["updated_at"] if row else None}
    except sqlite3.Error:
        return {"indexed": False, "records": 0, "updated_at": None}


def query_catalog(db_path: str, query: str, language: str | None, limit: int) -> list[Book]:
    if not catalog_status(db_path)["indexed"]:
        return []
    words = normalize(query).split()[:16]
    if not words:
        return []
    conditions, values = [], []
    for word in words:
        pattern = "%" + word.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"
        conditions.append("(title_search LIKE ? ESCAPE '\\' OR author_search LIKE ? ESCAPE '\\')")
        values.extend([pattern, pattern])
    if language:
        conditions.append("language LIKE ?")
        values.append("%;" + language + ";%")
    sql = "SELECT ebook_id,title,authors,language,issued FROM gutenberg_catalog WHERE " + \
          " AND ".join(conditions) + " ORDER BY ebook_id LIMIT ?"
    with connect(db_path) as db:
        rows = db.execute(sql, (*values, limit)).fetchall()
    books = []
    for row in rows:
        languages = [x for x in row["language"].strip(";").split(";") if x]
        lang = language if language and language in languages else (languages[0] if len(languages) == 1 else None)
        # Gutenberg CSV "Issued" means *catalog release*, not original
        # publication year. Do not misrepresent it as the book's year.
        books.append(Book(
            source="gutenberg_catalog", source_id=str(row["ebook_id"]),
            title=row["title"], authors=[name.strip() for name in row["authors"].split(";") if name.strip()],
            language=lang, year=None, cover_url=None,
            info_url=f"https://www.gutenberg.org/ebooks/{row['ebook_id']}",
            availability="catalog_only_epub_unverified",
        ))
    return books


class GutenbergCatalog(Provider):
    name = "gutenberg_catalog"
    purpose = "local_metadata"
    min_interval = 0.0

    def __init__(self, db_path: str):
        super().__init__()
        self.db_path = db_path

    async def search(self, client: httpx.AsyncClient, query: str, language: str | None, limit: int) -> list[Book]:
        # No network call. The catalog is loaded by an explicit administrative command.
        return await asyncio.to_thread(query_catalog, self.db_path, query, language, limit)


async def download_catalog_file(dest: Path) -> None:
    dest.parent.mkdir(parents=True, exist_ok=True)
    async with httpx.AsyncClient(timeout=httpx.Timeout(45.0), follow_redirects=False,
                                 headers={"User-Agent": "RoyalBooks/0.4 (https://github.com/TimeLance89/RoyalBooks)"}) as client:
        async with client.stream("GET", CATALOG_URL) as response:
            response.raise_for_status()
            if response.status_code != 200:
                raise ValueError("catalog_download_redirect_or_error")
            length = response.headers.get("content-length", "")
            if length.isdigit() and int(length) > MAX_ARCHIVE_BYTES:
                raise ValueError("gutenberg_archive_too_large")
            downloaded = 0
            with dest.open("xb") as out:
                async for block in response.aiter_bytes(65536):
                    downloaded += len(block)
                    if downloaded > MAX_ARCHIVE_BYTES:
                        raise ValueError("gutenberg_archive_too_large")
                    out.write(block)


def main():
    parser = argparse.ArgumentParser(description="Import official Gutenberg CSV locally; network is opt-in")
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--file", type=Path, help="Existing pg_catalog.csv or pg_catalog.csv.gz")
    group.add_argument("--download", action="store_true",
                       help="Fetch official weekly CSV.gz once (explicit opt-in)")
    parser.add_argument("--db", default=os.getenv("ROYALBOOKS_DB_PATH", "/data/royalbooks.sqlite3"))
    args = parser.parse_args()
    if args.download:
        # Temporary path ends with .gz so the decoder validates the compressed input.
        with tempfile.TemporaryDirectory(prefix="royalbooks-catalog-") as folder:
            path = Path(folder) / "pg_catalog.csv.gz"
            asyncio.run(download_catalog_file(path))
            count = import_catalog(path, args.db)
    else:
        count = import_catalog(args.file, args.db)
    print(f"Imported {count} Gutenberg text records into local search index: {args.db}")
    print("The catalog contains NO verified EPUB URLs or country-specific permission to download.")


if __name__ == "__main__":
    main()
