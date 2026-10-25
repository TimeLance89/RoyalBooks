"""Search UI grouping: editions preserved and titles not falsely merged."""
from royalbooks.presentation import display_groups
from royalbooks.providers import Book


def item(source, title, author="Rowling, J. K.", language="de", isbn=None, epub=None, cover=None):
    return Book(source=source, source_id=source + (isbn or ''), title=title,
                authors=[author] if author else [], language=language,
                year=None, cover_url=cover, info_url="https://example.org",
                isbn13=isbn, epub_url=epub)


def test_screenshot_case_groups_potter_editions_without_losing_them():
    books = [
        item("openlibrary", "Harry Potter and the Goblet of Fire", "J. K. Rowling", "en",
             cover="https://covers.openlibrary.org/a.jpg"),
        item("dnb", "Harry Potter und der Feuerkelch", "Rowling, J. K.", "de", isbn="9783551559122"),
        item("dnb", "Harry Potter und der Feuerkelch", "J. K. Rowling", "de", isbn="9783551559283"),
        item("dnb", "Harry Potter und der Feuerkelch", "", "de", isbn="9783551559043"),
        item("dnb", "Harry Potter und der Feuerkelch (illustrierte Ausgabe)", "Rowling, J. K.", "de"),
    ]
    groups = display_groups(books, "harry potter und der feuerkelch")
    assert len(groups) == 3
    assert groups[0]["item_indexes"] == [1, 2, 3]
    assert groups[0]["epub_count"] == 0
    assert sorted(i for g in groups for i in g["item_indexes"]) == list(range(5))


def test_different_authors_and_languages_not_grouped():
    books = [
        item("a", "Der Prozess", "Kafka, Franz", "de"),
        item("b", "Der Prozess", "Schmidt, Hans", "de"),
        item("c", "Der Prozess", "Franz Kafka", "en"),
    ]
    assert len(display_groups(books, "Der Prozess")) == 3


def test_available_epub_can_be_primary_without_losing_other_source():
    books = [
        item("dnb", "Faust", "Goethe, Johann", "de", isbn="9780140328721"),
        item("wikisource", "Faust", "Johann Goethe", "de", epub="https://example.org/epub"),
    ]
    groups = display_groups(books, "Faust")
    assert len(groups) == 1
    assert groups[0]["primary_index"] == 1
    assert groups[0]["epub_count"] == 1
    assert groups[0]["item_indexes"] == [0, 1]


def test_missing_language_or_missing_authors_stays_separate_without_evidence():
    books = [
        item("dnb", "Same Book", "", "de"),
        item("openlibrary", "Same Book", "", "de"),
        item("dnb", "Same Book", "Somebody", None),
    ]
    assert len(display_groups(books, "Same Book")) == 3


def test_exact_query_first_and_original_index_preserved():
    books = [
        item("a", "The Hogwarts Companion", "Author", "en", epub="https://example.org/book"),
        item("b", "Harry Potter and the Goblet of Fire", "J. K. Rowling", "en"),
    ]
    groups = display_groups(books, "Harry Potter and the Goblet of Fire")
    assert groups[0]["primary_index"] == 1
    assert groups[1]["primary_index"] == 0


def test_relevant_epub_offer_precedes_exact_metadata_only_title():
    books = [item('dnb', 'Faust', 'Goethe'),
             item('gutenberg_catalog', 'Faust: Eine Tragödie', 'Goethe', epub='https://example.org/book.epub')]
    assert display_groups(books, 'Faust')[0]['primary_index'] == 1
