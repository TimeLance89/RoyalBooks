"""Centrally constructed, keyless built-ins plus optional metadata extensions."""
from __future__ import annotations
import os
from royalbooks.providers import Provider, OpenLibrary, Gutendex, GoogleBooks
from royalbooks.dnb import DNB
from royalbooks.direct_sources import Wikisource, StandardEbooks
from royalbooks.gutenberg_catalog import GutenbergCatalog
from royalbooks.builtin_search import LibraryGenesis
from royalbooks.memory_source import MemoryOfTheWorld
from royalbooks.byte_source import ByteTo


def configured_providers() -> list[Provider]:
    path = os.getenv('ROYALBOOKS_DB_PATH', '/data/royalbooks.sqlite3')
    providers = [ByteTo(path), LibraryGenesis(path), MemoryOfTheWorld(path), DNB(), OpenLibrary(), Wikisource(path),
                 StandardEbooks(path), Gutendex(), GutenbergCatalog(path)]
    if key := os.getenv('GOOGLE_BOOKS_API_KEY', '').strip():
        providers.append(GoogleBooks(key))
    return providers
