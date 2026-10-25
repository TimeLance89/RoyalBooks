import asyncio
import json

import httpx
import pytest

from royalbooks.benchmark import is_target, load_cases, probe, run_benchmark
from royalbooks.providers import Book, Provider


def book(title='Die Verwandlung', language='de', author='Kafka, Franz'):
    return Book(source='mock', source_id='x1', title=title, authors=[author],
                language=language, year=None, cover_url=None, info_url='https://example.org')


class Stub(Provider):
    name = 'stub'
    min_interval = 0

    async def search(self, client, query, language, limit):
        if query == 'down':
            raise RuntimeError('api unavailable')
        return [book()]


def test_benchmark_language_not_assumed_and_exact_title(tmp_path):
    case = {'query':'Verwandlung','title':'Die Verwandlung','language':'de'}
    assert is_target(book(), case)
    assert not is_target(book(language=None), case)
    assert not is_target(book(title='Die Verwandlung 2'), {'title':'Die Verwandlung 3','language':'de'})
    path = tmp_path / 'cases.json'
    path.write_text(json.dumps([case]))
    assert load_cases(path) == [case]


def test_benchmark_rejects_bad_input(tmp_path):
    path = tmp_path / 'bad.json'
    path.write_text('[{"query":"x","title":"test","language":"xy"}]')
    with pytest.raises(ValueError):
        load_cases(path)


@pytest.mark.asyncio
async def test_benchmark_error_isolated_and_metrics_honest():
    async with httpx.AsyncClient(transport=httpx.MockTransport(lambda _: httpx.Response(200))) as client:
        report = await run_benchmark([
            {'query':'Verwandlung','title':'Die Verwandlung','language':'de'},
            {'query':'down','title':'Die Verwandlung','language':'de'},
        ], [Stub()], client)
    assert report['cases_count'] == 2
    assert report['summary'][0]['target_matches'] == 1
    assert report['summary'][0]['errors'] == 1
    assert report['summary'][0]['catalog_epub_link_cases'] == 0
    assert 'verified downloads' in report['notice']
