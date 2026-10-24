"""Independent catalog adapters. A catalog record is never proof of a legal download."""
from __future__ import annotations

import asyncio
from dataclasses import asdict, dataclass
from time import monotonic
from typing import Any

import httpx


class ProviderError(Exception):
    """One source failed, without stopping other sources."""


@dataclass(frozen=True)
class Book:
    source: str
    source_id: str
    title: str
    authors: list[str]
    language: str | None
    year: int | None
    cover_url: str | None
    info_url: str
    epub_url: str | None = None
    availability: str = "metadata_only"

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


class Provider:
    name = "unknown"
    purpose = "metadata"
    min_interval = 1.1

    def __init__(self) -> None:
        self._lock = asyncio.Lock()
        self._last_request = 0.0

    async def get_json(self, client: httpx.AsyncClient, url: str, params: dict[str, Any]) -> dict[str, Any]:
        # Serialized per source; Open Library explicitly limits anonymous clients to 1 req/s.
        async with self._lock:
            delay = max(0, self.min_interval - (monotonic() - self._last_request))
            if delay:
                await asyncio.sleep(delay)
            for attempt in range(3):
                self._last_request = monotonic()
                try:
                    response = await client.get(url, params=params)
                    if response.status_code in {429, 500, 502, 503, 504} and attempt < 2:
                        await asyncio.sleep(min(2.0, 0.5 * 2**attempt))
                        continue
                    response.raise_for_status()
                    payload = response.json()
                    if not isinstance(payload, dict):
                        raise ProviderError("invalid_json_shape")
                    return payload
                except (httpx.RequestError, ValueError) as exc:
                    if attempt == 2:
                        raise ProviderError(type(exc).__name__) from exc
                    await asyncio.sleep(min(2.0, 0.5 * 2**attempt))
                except httpx.HTTPStatusError as exc:
                    raise ProviderError(f"http_{exc.response.status_code}") from exc
            raise ProviderError("retry_exhausted")

    async def search(self, client: httpx.AsyncClient, query: str, language: str | None, limit: int) -> list[Book]:
        raise NotImplementedError


def _language(values: object) -> str | None:
    mapping = {"ger": "de", "deu": "de", "de": "de", "eng": "en", "en": "en"}
    if not isinstance(values, list) or not values:
        return None
    first = next((v for v in values if isinstance(v, str)), None)
    return mapping.get(first, first) if first else None


def _str_list(values: object) -> list[str]:
    return [v for v in values if isinstance(v, str)] if isinstance(values, list) else []


class Gutendex(Provider):
    name = "gutendex"
    purpose = "free_catalog"
    min_interval = 0.5

    async def search(self, client: httpx.AsyncClient, query: str, language: str | None, limit: int) -> list[Book]:
        params: dict[str, Any] = {"search": query}
        if language:
            params["languages"] = language
        data = await self.get_json(client, "https://gutendex.com/books", params)
        books: list[Book] = []
        for item in data.get("results", [])[:limit]:
            if not isinstance(item, dict) or not isinstance(item.get("id"), int) or not isinstance(item.get("title"), str):
                continue
            raw_formats = item.get("formats") or {}
            formats = raw_formats if isinstance(raw_formats, dict) else {}
            epub = formats.get("application/epub+zip")
            # Gutenberg marks copyright status for the US; users must check local law.
            authorized = item.get("copyright") is False
            if not (isinstance(epub, str) and epub.startswith("https://") and authorized):
                epub = None
            authors = [a["name"] for a in item.get("authors", []) if isinstance(a, dict) and isinstance(a.get("name"), str)]
            cover = formats.get("image/jpeg")
            books.append(Book(
                source=self.name, source_id=str(item["id"]), title=item["title"], authors=authors,
                language=_language(item.get("languages")), year=None,
                cover_url=cover if isinstance(cover, str) and cover.startswith("https://") else None,
                info_url=f"https://www.gutenberg.org/ebooks/{item['id']}",
                epub_url=epub,
                availability="public_domain_us_check_local_law" if epub else "metadata_only",
            ))
        return books


class OpenLibrary(Provider):
    name = "openlibrary"
    purpose = "metadata"

    async def search(self, client: httpx.AsyncClient, query: str, language: str | None, limit: int) -> list[Book]:
        params: dict[str, Any] = {"q": query, "limit": limit, "fields": "key,title,author_name,first_publish_year,language,cover_i"}
        if language:
            params["lang"] = language  # Ranking preference, not a strict filter.
        data = await self.get_json(client, "https://openlibrary.org/search.json", params)
        books: list[Book] = []
        for item in data.get("docs", [])[:limit]:
            if not isinstance(item, dict) or not isinstance(item.get("title"), str):
                continue
            key = item.get("key", "")
            if not isinstance(key, str) or not key.startswith("/works/") or not key.removeprefix("/works/").isalnum():
                continue
            lang = _language(item.get("language"))
            if language and lang and language not in _str_list(item.get("language")) and not any(
                code in _str_list(item.get("language")) for code in ({"de": ["ger", "deu"], "en": ["eng"]}.get(language, []))
            ):
                continue
            cover_id = item.get("cover_i")
            books.append(Book(
                source=self.name, source_id=key.removeprefix("/works/"),
                title=item["title"], authors=_str_list(item.get("author_name")), language=language if language and lang else lang,
                year=item.get("first_publish_year") if isinstance(item.get("first_publish_year"), int) else None,
                cover_url=f"https://covers.openlibrary.org/b/id/{cover_id}-M.jpg" if isinstance(cover_id, int) else None,
                info_url=f"https://openlibrary.org{key}",
            ))
        return books
