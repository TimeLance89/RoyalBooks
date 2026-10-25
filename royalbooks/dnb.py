"""DNB SRU/MARC21 bibliographic metadata adapter (no ebook downloads).

The German National Library returns bibliographic records, not permission to copy
a work. Keep this provider separate from acquisition providers.
"""
from __future__ import annotations

import asyncio
import re
from time import monotonic
from xml.etree import ElementTree as ET

import httpx
from defusedxml import ElementTree as SafeET

from royalbooks.providers import Book, Provider, ProviderError, isbn13, valid_isbn10, canonical_isbn

SRU = "https://services.dnb.de/sru/dnb"
MARC = "{http://www.loc.gov/MARC21/slim}"
MAX_XML_BYTES = 2 * 1024 * 1024
TRANSIENT = {429, 500, 502, 503, 504}


def _subfields(record: ET.Element, tag: str, codes: tuple[str, ...] = ("a",)) -> list[str]:
    values = []
    for field in record.findall(f"{MARC}datafield"):
        if field.get("tag") != tag:
            continue
        for element in field.findall(f"{MARC}subfield"):
            if element.get("code") in codes and element.text and element.text.strip():
                values.append(''.join(c for c in element.text if ord(c) >= 32 and not 127 <= ord(c) <= 159).strip())
    return values


def _control(record: ET.Element, tag: str) -> str | None:
    for el in record.findall(f"{MARC}controlfield"):
        if el.get("tag") == tag and el.text:
            # MARC controlfield 008 is fixed-width: whitespace is semantically significant.
            return el.text if tag == "008" else el.text.strip()
    return None


def parse_records(xml: bytes, limit: int, language: str | None) -> list[Book]:
    if len(xml) > MAX_XML_BYTES:
        raise ProviderError("dnb_response_too_large")
    try:
        root = SafeET.fromstring(xml)
    except (ET.ParseError, ValueError) as exc:
        raise ProviderError("invalid_dnb_xml") from exc
    records: list[Book] = []
    for record in root.findall(f".//{MARC}record"):
        source_id = _control(record, "001")
        if not source_id or not re.fullmatch(r"[0-9X]{5,20}", source_id):
            continue
        title_parts = _subfields(record, "245", ("a", "b"))
        if not title_parts:
            continue
        title = " ".join(title_parts).strip(" /.:; ")
        if not title:
            continue
        authors = _subfields(record, "100") or _subfields(record, "700")
        authors = [a.strip(" /,;") for a in authors[:4]]
        langs = _subfields(record, "041")
        marker = langs[0][:3].lower() if langs else None
        control008 = _control(record, "008")
        if not marker and control008 and len(control008) >= 38:
            marker = control008[35:38].lower()
        parsed_lang = {"ger": "de", "deu": "de", "eng": "en"}.get(marker, None)
        if len(set(langs)) > 1:
            parsed_lang = None
        # If a record explicitly carries a different language, don't mislabel it.
        if language and parsed_lang and parsed_lang != language:
            continue
        dates = _subfields(record, "264", ("c",)) or _subfields(record, "260", ("c",))
        match = re.search(r"(?:18|19|20)\d{2}", dates[0]) if dates else None
        isbn_values = []
        for raw in _subfields(record, "020"):
            cleaned = re.sub(r"[^0-9X]", "", raw.split("(")[0].upper())
            isbn_values.append(cleaned)
        books_isbn = isbn13(isbn_values) or next((canonical_isbn(n) for n in isbn_values if valid_isbn10(n)), None)
        records.append(Book(
            source="dnb", source_id=source_id, title=title, authors=authors,
            language=parsed_lang, year=int(match.group()) if match else None,
            cover_url=None, info_url=f"https://d-nb.info/{source_id}", isbn13=books_isbn,
            isbns=tuple(dict.fromkeys(n for n in isbn_values if isbn13([n]) or valid_isbn10(n))),
            description=' '.join(_subfields(record, '520'))[:6000] or None,
        ))
        if len(records) >= limit:
            break
    return records


class DNB(Provider):
    name = "dnb"
    purpose = "metadata"
    min_interval = 1.5

    async def search(self, client: httpx.AsyncClient, query: str, language: str | None, limit: int) -> list[Book]:
        # No CQL grammar from the input is accepted, only a conservative text query.
        words = re.findall(r"[\w-]+", query, flags=re.UNICODE)
        if not words:
            return []
        safe_title = " ".join(words)[:120]
        compact = re.sub(r'[^0-9X]', '', query.upper())
        if isbn13([compact]):
            cql = f'num="{compact}"'
        elif ' author:' in query:
            title, author = query.split(' author:', 1)
            title = ' '.join(re.findall(r'[\w-]+', title))
            author = ' '.join(re.findall(r'[\w-]+', author))
            cql = f'tit all "{title}" and per all "{author}" and mat=books'
        else:
            # The default index includes both title and author, unlike tit.
            cql = f'woe all "{safe_title}" and mat=books'
        if language in ("de", "en"):
            cql += f' and spr={"ger" if language == "de" else "eng"}'
        params = {"version": "1.1", "operation": "searchRetrieve", "recordSchema": "MARC21-xml",
                  "maximumRecords": min(limit, 30), "query": cql}
        async with self._lock:
            delay = max(0.0, self.min_interval - (monotonic() - self._last_request))
            if delay:
                await asyncio.sleep(delay)
            for attempt in range(3):
                self._last_request = monotonic()
                try:
                    response = await client.get(SRU, params=params, headers={"Accept": "application/xml"})
                    if response.status_code in TRANSIENT and attempt < 2:
                        await asyncio.sleep(0.5 * (2 ** attempt))
                        continue
                    response.raise_for_status()
                    return parse_records(response.content, limit, language)
                except httpx.HTTPStatusError as exc:
                    raise ProviderError(f"dnb_http_{exc.response.status_code}") from exc
                except httpx.RequestError as exc:
                    if attempt == 2:
                        raise ProviderError(type(exc).__name__) from exc
                    await asyncio.sleep(0.5 * (2 ** attempt))
        raise ProviderError("dnb_retry_exhausted")
