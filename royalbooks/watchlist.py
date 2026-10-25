"""Persistent private reading list backed by SQLite.

A saved catalog record is NOT an acquisition request. Saving never downloads
books or implies that a licensed EPUB has been located.
"""
from __future__ import annotations

import json
import sqlite3
from royalbooks.database import connect as db_connect
import uuid
from pathlib import Path


MAX_ENTRIES = 1000


class WatchlistFull(Exception):
    pass


class WatchlistStore:
    def __init__(self, db_path: str):
        self.db_path = db_path
        Path(db_path).parent.mkdir(parents=True, exist_ok=True)
        with self._db() as db:
            db.execute("""CREATE TABLE IF NOT EXISTS watchlist (
                id TEXT PRIMARY KEY,
                source TEXT NOT NULL,
                source_id TEXT NOT NULL,
                title TEXT NOT NULL,
                authors_json TEXT NOT NULL,
                language TEXT,
                info_url TEXT,
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                UNIQUE(source,source_id)
            )""")
            db.execute("CREATE INDEX IF NOT EXISTS idx_watchlist_created_at ON watchlist(created_at)")

    def _db(self) -> sqlite3.Connection:
        db = db_connect(self.db_path, timeout=10)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA busy_timeout=10000")
        return db

    @staticmethod
    def _read(row) -> dict:
        item = dict(row)
        item["authors"] = json.loads(item.pop("authors_json"))
        return item

    def add(self, *, source: str, source_id: str, title: str,
            authors: list[str], language: str | None, info_url: str | None) -> dict:
        with self._db() as db:
            db.execute("BEGIN IMMEDIATE")
            existing = db.execute("SELECT * FROM watchlist WHERE source=? AND source_id=?",
                                  (source, source_id)).fetchone()
            if existing:
                return self._read(existing)
            count = db.execute("SELECT COUNT(*) FROM watchlist").fetchone()[0]
            if count >= MAX_ENTRIES:
                raise WatchlistFull("watchlist_capacity_reached")
            ident = uuid.uuid4().hex
            db.execute("""INSERT INTO watchlist(id,source,source_id,title,authors_json,language,info_url)
                         VALUES (?,?,?,?,?,?,?)""",
                       (ident, source, source_id, title.strip(), json.dumps(authors),
                        language, info_url))
            return self._read(db.execute("SELECT * FROM watchlist WHERE id=?", (ident,)).fetchone())

    def list(self) -> list[dict]:
        with self._db() as db:
            rows = db.execute("SELECT * FROM watchlist ORDER BY created_at DESC, rowid DESC LIMIT ?",
                              (MAX_ENTRIES,)).fetchall()
            return [self._read(row) for row in rows]

    def remove(self, ident: str) -> bool:
        with self._db() as db:
            return db.execute("DELETE FROM watchlist WHERE id=?", (ident,)).rowcount == 1
