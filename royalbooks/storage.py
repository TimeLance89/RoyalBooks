"""Simple persistent provider diagnostic events; no search terms or personal data stored."""
from __future__ import annotations

import sqlite3
from pathlib import Path


class MetricsStore:
    def __init__(self, path: str) -> None:
        self.path = path
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as db:
            db.execute("""CREATE TABLE IF NOT EXISTS attempts (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                timestamp TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
                provider TEXT NOT NULL,
                outcome TEXT NOT NULL CHECK (outcome IN ('found', 'empty', 'error')),
                result_count INTEGER NOT NULL,
                duration_ms INTEGER NOT NULL,
                error_code TEXT
            )""")

    def _connect(self) -> sqlite3.Connection:
        db = sqlite3.connect(self.path, timeout=5)
        db.execute("PRAGMA busy_timeout=5000")
        return db

    def record(self, provider: str, outcome: str, count: int, duration_ms: int, error_code: str | None = None) -> None:
        with self._connect() as db:
            db.execute("INSERT INTO attempts(provider, outcome, result_count, duration_ms, error_code) VALUES (?,?,?,?,?)",
                       (provider, outcome, count, duration_ms, error_code))

    def stats(self) -> list[dict]:
        with self._connect() as db:
            rows = db.execute("""SELECT provider, COUNT(*), SUM(outcome='found'), SUM(outcome='empty'),
                SUM(outcome='error'), ROUND(AVG(duration_ms)), SUM(result_count)
                FROM (SELECT * FROM attempts ORDER BY id DESC LIMIT 200) GROUP BY provider ORDER BY provider""").fetchall()
        return [dict(provider=r[0], attempts=r[1], found=r[2], empty=r[3], errors=r[4],
                     avg_duration_ms=r[5], result_count=r[6]) for r in rows]
