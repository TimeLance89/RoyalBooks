"""Conservative catalog overlap discovery; never equate a work with an edition."""
from __future__ import annotations

import re
import unicodedata
from collections import defaultdict
from typing import Iterable

from royalbooks.providers import Book


def normalize(value: str) -> str:
    folded = unicodedata.normalize("NFKD", value.casefold())
    plain = "".join(c for c in folded if not unicodedata.combining(c))
    return " ".join(re.findall(r"[^\W_]+", plain, flags=re.UNICODE))


def normalize_person(value: str) -> str:
    # German library catalogs often use "Nachname, Vorname" rather than "Vorname Nachname".
    if "," in value:
        surname, given = value.split(",", 1)
        return normalize(given + " " + surname)
    return normalize(value)


def match_groups(books: Iterable[Book]) -> list[dict]:
    """Conservative bibliographic candidates; don't merge entries or initiate downloads.

    Same ISBN-13 + title + language: edition candidate. Same title + primary
    author + language: work candidate; editions may differ. Missing language
    prevents matching. Both kinds require corroboration by separate providers.
    """
    books = list(books)
    buckets = defaultdict(list)
    for index, book in enumerate(books):
        title = normalize(book.title)
        author = normalize_person(book.authors[0]) if book.authors else ""
        lang = book.language if book.language in ("de", "en") else ""
        if not title or not lang:
            continue
        if book.isbn13:
            buckets[("isbn13", book.isbn13, lang, title)].append(index)
        if author:
            buckets[("work", title, author, lang)].append(index)
    groups = []
    for key, indexes in buckets.items():
        sources = {books[i].source for i in indexes}
        if len(indexes) < 2 or len(sources) < 2:
            continue
        groups.append({
            "kind": "edition_candidate" if key[0] == "isbn13" else "work_candidate",
            "evidence": "matching_isbn13_title_language" if key[0] == "isbn13" else "matching_title_author_language",
            "item_indexes": indexes,
            "sources": sorted(sources),
            "notice": "Bibliographic candidate only; no automatic download or merging.",
        })
    return sorted(groups, key=lambda group: (group["kind"] != "edition_candidate", group["item_indexes"]))
