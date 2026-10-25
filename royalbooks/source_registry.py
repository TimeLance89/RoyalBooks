"""Single source registry shared by the API and opt-in benchmark runner."""
from __future__ import annotations

import os

from royalbooks.direct_sources import StandardEbooks, Wikisource
from royalbooks.dnb import DNB
from royalbooks.gutenberg_catalog import GutenbergCatalog
from royalbooks.providers import GoogleBooks, Gutendex, OpenLibrary, Provider


def configured_providers() -> list[Provider]:
    providers: list[Provider] = [Gutendex(), OpenLibrary()]
    # Offline Gutenberg index is independent of Gutendex availability.
    providers.append(GutenbergCatalog(os.getenv("ROYALBOOKS_DB_PATH", "/data/royalbooks.sqlite3")))
    providers.extend([Wikisource(os.getenv("ROYALBOOKS_DB_PATH", "/data/royalbooks.sqlite3")),
                      StandardEbooks(os.getenv("ROYALBOOKS_DB_PATH", "/data/royalbooks.sqlite3"))])
    if os.getenv("ROYALBOOKS_DNB_ENABLED", "1").lower() not in {"0", "false", "no"}:
        providers.append(DNB())
    key = os.getenv("GOOGLE_BOOKS_API_KEY", "").strip()
    if key:
        providers.append(GoogleBooks(key))
    return providers
