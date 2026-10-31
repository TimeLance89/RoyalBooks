"""Search presentation only. Never merges or discards edition-level catalog records.

The API continues exposing every raw book under 'items'; grouped cards merely
provide a browsable, reversible overview of similarly titled catalog records.
"""
from __future__ import annotations

from collections import defaultdict

from royalbooks.matching import normalize, normalize_person
from royalbooks.providers import Book


def _surname(book: Book) -> str:
    if not book.authors:
        return ""
    return normalize_person(book.authors[0])


def _primary_score(book: Book) -> tuple[int, int, int, int]:
    """Prefer a downloadable entry, otherwise a useful illustrated listing."""
    return (int(bool(book.epub_url or book.acquisition_id)), int(bool(book.cover_url)),
            int(bool(book.authors)), int(bool(book.isbn13)))


def display_groups(books: list[Book], query: str = "") -> list[dict]:
    buckets: dict[tuple[str, str], list[int]] = defaultdict(list)
    for index, book in enumerate(books):
        title = normalize(book.title)
        # Unknown-language records stay isolated; do not say they are
        # the same translation simply because they share a title.
        if not title or not book.language:
            buckets[(f"unknown-{index}", "")].append(index)
        else:
            buckets[(title, book.language)].append(index)

    groups: list[dict] = []
    for _, indexes in buckets.items():
        by_author: dict[str, list[int]] = defaultdict(list)
        missing: list[int] = []
        for index in indexes:
            surname = _surname(books[index])
            if surname:
                by_author[surname].append(index)
            else:
                missing.append(index)

        if len(by_author) == 1:
            # Attach missing-author records only when there's a single
            # plausible surname for this exact title/language.
            next(iter(by_author.values())).extend(missing)
            batches = list(by_author.values())
        elif len(by_author) > 1:
            # Different named authors must not collapse into one work.
            batches = list(by_author.values()) + [[i] for i in missing]
        else:
            # No author information: don't assert that same titles
            # represent the same work; keep them individually accessible.
            batches = [[i] for i in missing]

        for indexes_for_group in batches:
            indexes_for_group.sort()
            primary = max(indexes_for_group, key=lambda i: _primary_score(books[i]))
            groups.append({
                "primary_index": primary,
                "item_indexes": indexes_for_group,
                "epub_count": sum(bool(books[i].epub_url or books[i].acquisition_id) for i in indexes_for_group),
                "notice": "Visual grouping of similar catalog records; ISBNs and editions remain separate.",
            })

    needle = normalize(query)
    def rank(group: dict) -> tuple[int, int, int]:
        title = normalize(books[group["primary_index"]].title)
        if title == needle:
            relevance = 0
        elif needle and needle in title:
            relevance = 1
        else:
            relevance = 2
        words = set(needle.split())
        book = books[group['primary_index']]
        content = set(normalize(book.title + ' ' + ' '.join(book.authors)).split())
        overlap = len(words & content) / len(words) if words else 0
        return (relevance > 1, -int(group['epub_count'] > 0), relevance, -overlap,
                min(group["item_indexes"]))

    return sorted(groups, key=rank)
