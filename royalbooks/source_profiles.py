"""Source capabilities and evidence boundaries for RoyalBooks catalog search.

A bibliographic listing is not a downloadable file. A catalog-listed EPUB is not
a verified download. Source profiles describe API behavior, not content rights.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass

from royalbooks.providers import Book


@dataclass(frozen=True)
class SourceProfile:
    name: str
    display_name: str
    role: str
    edition_granularity: str
    languages: str
    listed_formats: tuple[str, ...]
    may_offer_epub: bool
    note: str

    def as_dict(self, *, enabled: bool) -> dict:
        data = asdict(self)
        data["listed_formats"] = list(self.listed_formats)
        data["enabled"] = enabled
        return data


PROFILES = {
    'webshare': SourceProfile('webshare', 'Webshare', 'verified_epub', 'edition',
                             'epub_metadata_verified', ('EPUB',), True,
                             'Kostenlos ohne Konto; vollständige EPUB- und Sprachprüfung vor der Anzeige. Bis zu zwei neue Dateien je Suche, 24 Stunden zwischengespeichert.'),
    'byte': SourceProfile('byte','Byte.to','hoster_catalog','release','unknown_until_file_check',(),False,
                          'Source-listed link containers; separate downloader, hoster waits and captchas may apply'),
    'memoryoftheworld': SourceProfile('memoryoftheworld', 'Memory of the World',
                           'file_listing', 'edition', 'multilingual', ('EPUB',), True,
                           'Independent community library; source-listed EPUBs, no account required'),
    'libgen': SourceProfile('libgen', 'Library Genesis', 'file_listing', 'edition',
                           'multilingual', ('EPUB', 'PDF', 'AZW3', 'MOBI', 'FB2', 'DJVU', 'TXT'), True,
                           'Source-bound EPUB acquisition; fresh GET link and integrity checks required'),
    "dnb": SourceProfile("dnb", "Deutsche Nationalbibliothek", "bibliography",
                         "edition", "de_priority", (), False,
                         "German editions and ISBNs; no downloadable files"),
    "openlibrary": SourceProfile("openlibrary", "Open Library", "bibliography",
                                 "work", "multilingual", (), False,
                                 "Work record can cover many languages and editions"),
    "google_books": SourceProfile("google_books", "Google Books", "bibliography",
                                  "volume", "multilingual", (), False,
                                  "Book information and cover; not a file source"),
    "gutendex": SourceProfile("gutendex", "Gutendex", "epub_catalog",
                              "edition", "multilingual", ("EPUB",), True,
                              "Catalog link; fresh rights and file checks required"),
    "gutenberg_catalog": SourceProfile("gutenberg_catalog", "Project Gutenberg Offline",
                                       "epub_catalog", "edition", "multilingual",
                                       ("EPUB",), True,
                                       "Requires imported official CSV and RDF catalogs"),
    "wikisource": SourceProfile("wikisource", "Wikisource", "publisher_epub",
                                "edition", "de_en", ("EPUB",), True,
                                "Direct EPUB only when listed by the publisher"),
    "standardebooks": SourceProfile("standardebooks", "Standard Ebooks", "publisher_epub",
                                    "edition", "en", ("EPUB",), True,
                                    "English-language curated EPUB listings"),
}


def profiles_for(names) -> list[dict]:
    available = set(names)
    return [descriptor.as_dict(enabled=key in available)
            for key, descriptor in PROFILES.items()]


def search_evidence(items: list[Book]) -> dict:
    """Separate file listings from bibliographic records without HTTP assumptions."""
    listed = [item for item in items if bool(item.epub_url)]
    metadata = [item for item in items if not item.epub_url and not item.formats]
    unknown = sum(not item.language for item in items)
    return {
        "catalog_records": len(items),
        "epub_links_listed_unverified": len(listed),
        "epub_acquisition_candidates": sum(book.as_dict()['download_supported'] for book in items),
        "file_listings_unverified": sum(bool(item.formats) and not item.epub_url and
                                       item.as_dict()['file_evidence'] != 'verified_epub_cached' for item in items),
        "bibliographic_records_without_epub": len(metadata),
        "german_records_declared": sum(item.language == "de" for item in items),
        "unknown_language_records": unknown,
        "verified_downloads": sum(book.as_dict()['file_evidence'] == 'verified_epub_cached' for book in items),
        "explanation": "Catalog claims do not establish a working file, file language or permission to download.",
    }
