from royalbooks.source_profiles import PROFILES, profiles_for, search_evidence
from royalbooks.providers import Book


def book(source, title="Buch", language="de", epub=None):
    return Book(source=source,source_id="1",title=title,authors=["Autor"],
                language=language,year=None,cover_url=None,
                info_url="https://example.org",epub_url=epub)


def test_providers_divide_bibliography_and_epub_catalog():
    data = {p["name"]: p for p in profiles_for(["dnb", "wikisource"])}
    assert set(data) == set(PROFILES)
    assert data["dnb"]["role"] == "bibliography"
    assert data["dnb"]["may_offer_epub"] is False
    assert data["dnb"]["enabled"] is True
    assert data["openlibrary"]["edition_granularity"] == "work"
    assert data["openlibrary"]["enabled"] is False
    assert data["wikisource"]["may_offer_epub"] is True


def test_search_evidence_never_claims_file_was_downloaded():
    report = search_evidence([
        book("dnb"),
        book("openlibrary",language=None),
        book("wikisource",epub="https://example.org/title.epub"),
    ])
    assert report["catalog_records"] == 3
    assert report["bibliographic_records_without_epub"] == 2
    assert report["epub_links_listed_unverified"] == 1
    assert report["german_records_declared"] == 2
    assert report["unknown_language_records"] == 1
    assert report["verified_downloads"] == 0
    assert search_evidence([])["catalog_records"] == 0
