"""Revocable browser sessions and single-use device links; never store API tokens."""
import hashlib
import hmac
import secrets
import time

from royalbooks.database import connect

COOKIE = 'royalbooks_session'
SESSION_SECONDS = 7 * 24 * 3600
LINK_SECONDS = 15 * 60


def _digest(value):
    return hashlib.sha256(value.encode()).hexdigest()


def _binding(key):
    return hmac.new(key.encode(), b'royalbooks-browser-access-v1', hashlib.sha256).hexdigest()


class SessionStore:
    def __init__(self, path):
        self.path = path
        with connect(path, timeout=5) as db:
            for table in ('browser_sessions', 'device_links'):
                db.execute(f'''CREATE TABLE IF NOT EXISTS {table} (
                    digest TEXT PRIMARY KEY, binding TEXT NOT NULL, expires INTEGER NOT NULL)''')

    def _prune(self, db):
        for table in ('browser_sessions', 'device_links'):
            db.execute(f'DELETE FROM {table} WHERE expires <= ?', (int(time.time()),))

    def create_session(self, key):
        if not key:
            raise ValueError('access_disabled')
        value = secrets.token_urlsafe(32)
        with connect(self.path, timeout=5) as db:
            self._prune(db)
            db.execute('INSERT INTO browser_sessions VALUES (?,?,?)',
                       (_digest(value), _binding(key), int(time.time()) + SESSION_SECONDS))
        return value

    def authenticated(self, value, key):
        if not key or not value or len(value) > 128:
            return False
        with connect(self.path, timeout=5) as db:
            row = db.execute('SELECT binding,expires FROM browser_sessions WHERE digest=?',
                             (_digest(value),)).fetchone()
        return bool(row and row[1] > time.time() and hmac.compare_digest(row[0], _binding(key)))

    def logout(self, value):
        if value:
            with connect(self.path, timeout=5) as db:
                db.execute('DELETE FROM browser_sessions WHERE digest=?', (_digest(value),))

    def create_device_link(self, key):
        if not key:
            raise ValueError('access_disabled')
        value = secrets.token_urlsafe(32)
        with connect(self.path, timeout=5) as db:
            self._prune(db)
            db.execute('INSERT INTO device_links VALUES (?,?,?)',
                       (_digest(value), _binding(key), int(time.time()) + LINK_SECONDS))
        return value

    def consume_device_link(self, value, key):
        if not key or not value or len(value) > 128:
            return False
        with connect(self.path, timeout=5) as db:
            db.execute('BEGIN IMMEDIATE')
            row = db.execute('SELECT binding,expires FROM device_links WHERE digest=?',
                             (_digest(value),)).fetchone()
            if not row or row[1] <= time.time() or not hmac.compare_digest(row[0], _binding(key)):
                return False
            db.execute('DELETE FROM device_links WHERE digest=?', (_digest(value),))
        return True
