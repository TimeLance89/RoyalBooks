"""Single source registry shared by the API and opt-in benchmark runner."""
from __future__ import annotations

import os

from royalbooks.dnb import DNB
from royalbooks.providers import GoogleBooks, Gutendex, OpenLibrary, Provider


def configured_providers() -> list[Provider]:
    providers: list[Provider] = [Gutendex(), OpenLibrary()]
    if os.getenv("ROYALBOOKS_DNB_ENABLED", "1").lower() not in {"0", "false", "no"}:
        providers.append(DNB())
    key = os.getenv("GOOGLE_BOOKS_API_KEY", "").strip()
    if key:
        providers.append(GoogleBooks(key))
    return providers
