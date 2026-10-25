"""Resolve publisher-listed LibGen GET links; no guessed keys or challenge solving."""
from __future__ import annotations

import re
from html.parser import HTMLParser
from urllib.parse import parse_qs, urljoin, urlsplit

import httpx

from royalbooks.providers import ProviderError

CATALOG_HOSTS = ('libgen.li', 'libgen.gl', 'libgen.bz', 'libgen.la')
FILE_HOSTS = frozenset({'cdn1.booksdl.lc', 'cdn2.booksdl.lc'})
MD5 = re.compile(r'[0-9a-fA-F]{32}')
MAX_PAGE = 2 * 1024 * 1024


def _parts(url: str):
    if not isinstance(url, str) or len(url) > 1500:
        return None
    try:
        parts = urlsplit(url)
        if (parts.scheme != 'https' or parts.username or parts.password or parts.fragment
                or parts.port not in (None, 443)):
            return None
        return parts, parse_qs(parts.query, keep_blank_values=True)
    except ValueError:
        return None


def safe_catalog_url(url: str) -> bool:
    data = _parts(url)
    if not data:
        return False
    parts, args = data
    return (parts.hostname in CATALOG_HOSTS and parts.path == '/ads.php'
            and set(args) == {'md5'} and len(args['md5']) == 1
            and bool(MD5.fullmatch(args['md5'][0])))


def safe_file_url(url: str) -> bool:
    data = _parts(url)
    if not data:
        return False
    parts, args = data
    return (parts.hostname in {*CATALOG_HOSTS, *FILE_HOSTS} and parts.path == '/get.php'
            and set(args) == {'md5', 'key'} and len(args['md5']) == len(args['key']) == 1
            and bool(MD5.fullmatch(args['md5'][0]))
            and bool(re.fullmatch(r'[A-Za-z0-9_-]{8,128}', args['key'][0])))


class _Links(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.links = []

    def handle_starttag(self, tag, attrs):
        if tag == 'a':
            href = dict(attrs).get('href')
            if href:
                self.links.append(href)


def parse_get_link(page: str, catalog_url: str) -> str:
    if not safe_catalog_url(catalog_url):
        raise ProviderError('untrusted_libgen_catalog')
    wanted = parse_qs(urlsplit(catalog_url).query)['md5'][0].lower()
    parser = _Links()
    parser.feed(page)
    for href in parser.links:
        url = urljoin(catalog_url, href)
        if (safe_file_url(url) and urlsplit(url).hostname == urlsplit(catalog_url).hostname
                and parse_qs(urlsplit(url).query)['md5'][0].lower() == wanted):
            return url
    raise ProviderError('libgen_download_link_missing')


async def resolve_epub(client: httpx.AsyncClient, catalog_url: str) -> str:
    if not safe_catalog_url(catalog_url):
        raise ProviderError('untrusted_libgen_catalog')
    # Keys are read freshly from the source HTML and never persisted or logged.
    async with client.stream('GET', catalog_url, follow_redirects=False) as response:
        if response.status_code != 200:
            raise ProviderError(f'http_{response.status_code}')
        data = bytearray()
        async for chunk in response.aiter_bytes(65536):
            data.extend(chunk)
            if len(data) > MAX_PAGE:
                raise ProviderError('libgen_page_too_large')
    return parse_get_link(data.decode('utf-8', errors='replace'), catalog_url)
