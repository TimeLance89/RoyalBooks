from xml.sax.saxutils import escape

import httpx
import pytest

from royalbooks.dnb import DNB, parse_records
from royalbooks.providers import Book, ProviderError
from royalbooks.matching import match_groups, normalize


def marc_record(*, ident="123456789", lang="ger", title="Der Prozess", isbn="9780140328721"):
    return (f'<record xmlns="http://www.loc.gov/MARC21/slim">'
            f'<controlfield tag="001">{ident}</controlfield>'
            f'<controlfield tag="008">{" " * 35}{lang}{" " * 3}</controlfield>'
            f'<datafield tag="245"><subfield code="a">{escape(title)}</subfield></datafield>'
            '<datafield tag="100"><subfield code="a">Kafka, Franz</subfield></datafield>'
            f'<datafield tag="020"><subfield code="a">{isbn} (Broschur)</subfield></datafield>'
            '<datafield tag="264"><subfield code="c">[2022]</subfield></datafield></record>')


def wrap(*items):
    return ('<searchRetrieveResponse xmlns="http://www.loc.gov/zing/srw/">'
            '<records><record><recordData>' + ''.join(items) +
            '</recordData></record></records></searchRetrieveResponse>').encode()


def test_dnb_parse_marc_with_isbn_and_language():
    records = parse_records(wrap(marc_record()), 10, 'de')
    assert len(records) == 1
    assert records[0].source == "dnb"
    assert records[0].year == 2022
    assert records[0].isbn13 == "9780140328721"
    assert records[0].info_url == "https://d-nb.info/123456789"
    assert records[0].epub_url is None


def test_dnb_language_mismatch_and_oversized_xml():
    assert parse_records(wrap(marc_record(lang="eng")), 10, "de") == []
    with pytest.raises(ProviderError, match="too_large"):
        parse_records(b"X" * (2 * 1024 * 1024 + 1), 10, None)


def test_dnb_unsafe_xml_entities_rejected():
    malicious = b'<!DOCTYPE a [<!ENTITY lol "boom">]><a>&lol;</a>'
    with pytest.raises(Exception):
        parse_records(malicious, 10, None)


@pytest.mark.asyncio
async def test_dnb_query_cql_is_escaped_and_source_metadata_only():
    def handler(req):
        assert req.url.host == "services.dnb.de"
        query = req.url.params["query"]
        assert query.startswith('woe all "Harry Potter drop table"')
        assert "spr=ger" in query and "mat=books" in query
        return httpx.Response(200, content=wrap(marc_record()))
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        books = await DNB().search(client, 'Harry Potter") drop table', 'de', 10)
    assert books[0].availability == "metadata_only"


def mk(source, title="Der Prozess", author="Franz Kafka", lang="de", isbn=None):
    return Book(source=source,source_id="A",title=title,authors=[author],language=lang,
                year=None,cover_url=None,info_url="https://example.org",isbn13=isbn)


def test_matching_is_conservative_and_keeps_different_editions_separate():
    a = mk("openlibrary", isbn="9780140328721")
    b = mk("google_books", isbn="9780140328721")
    c = mk("dnb", isbn="9780140328738")
    groups = match_groups([a,b,c])
    assert len(groups) == 2
    assert groups[0]["kind"] == "edition_candidate"
    assert groups[0]["item_indexes"] == [0,1]
    assert groups[1]["kind"] == "work_candidate"
    assert groups[1]["item_indexes"] == [0,1,2]


def test_matching_does_not_cross_language_or_title():
    books = [mk("openlibrary"), mk("dnb", lang="en"), mk("google_books", title="Der andere Prozess")]
    assert match_groups(books) == []
    assert normalize("Ärger, ÄÖ") == "arger ao"
