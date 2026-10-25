"""SQLite transaction contexts that also release their file descriptors."""
import sqlite3


class ClosingConnection(sqlite3.Connection):
    def __exit__(self, *args):
        try:
            return super().__exit__(*args)
        finally:
            self.close()


def connect(*args, **kwargs):
    return sqlite3.connect(*args, factory=ClosingConnection, **kwargs)
