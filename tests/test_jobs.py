"""Deterministic acquisition tests: no external HTTP or NAS required."""
import asyncio
import io
import zipfile
from pathlib import Path

import httpx
import pytest

from royalbooks.app import app
from royalbooks.jobs import (AcquisitionError, JobStore, deliver_staged,
                             download_epub, process_queued, valid_url, validate_epub)


def example_epub(extra_name='OEBPS/chapter.xhtml'):
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, 'w') as archive:
        archive.writestr('mimetype', b'application/epub+zip', compress_type=zipfile.ZIP_STORED)
        archive.writestr('META-INF/container.xml',
                         '<container xmlns="urn:oasis:names:tc:opendocument:xmlns:container" version="1.0">'
                         '<rootfiles><rootfile full-path="OEBPS/content.opf" '
                         'media-type="application/oebps-package+xml"/></rootfiles></container>')
        archive.writestr('OEBPS/content.opf', '<package/>')
        archive.writestr(extra_name, '<html/>')
    return buffer.getvalue()


def test_trusted_urls_block_ssrf():
    assert valid_url('https://www.gutenberg.org/cache/epub/12/a.epub')
    for url in ['http://www.gutenberg.org/cache/epub/12/a.epub',
                'https://www.gutenberg.org.evil.org/ebooks/12.epub',
                'https://127.0.0.1/files/book.epub',
                'https://www.gutenberg.org:8080/ebooks/12.epub',
                'https://me:pw@www.gutenberg.org/ebooks/12.epub',
                'file:///etc/passwd']:
        assert not valid_url(url)


def test_epub_validation_and_traversal_rejection(tmp_path):
    path = tmp_path / 'book.epub'
    path.write_bytes(example_epub())
    assert len(validate_epub(path)) == 64
    path.write_bytes(example_epub('../evil.xhtml'))
    with pytest.raises(AcquisitionError, match='invalid_archive_path'):
        validate_epub(path)
    path.write_bytes(b'NOT A ZIP')
    with pytest.raises(AcquisitionError, match='invalid_epub_archive'):
        validate_epub(path)


def test_job_dedup_and_recovery(tmp_path):
    path = str(tmp_path / 'jobs.sqlite3')
    store = JobStore(path)
    a = store.add(42)
    assert store.add(42)['id'] == a['id']
    assert store.claim(('queued',))['id'] == a['id']
    b = store.add(43)
    assert store.claim(('queued',))['id'] == b['id']
    store.update(b['id'], 'delivering')
    recovered = JobStore(path)
    jobs = {job['source_id']: job for job in recovered.list()}
    assert jobs[42]['status'] == 'queued'
    assert jobs[43]['status'] == 'needs_review'


@pytest.mark.asyncio
async def test_download_disallows_redirect_offsite(tmp_path):
    def handler(request):
        return httpx.Response(302, headers={'location': 'http://192.168.0.1/admin'})
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(AcquisitionError, match='untrusted_redirect'):
            await download_epub(client, 'https://www.gutenberg.org/ebooks/42.epub', tmp_path / 'out')


@pytest.mark.asyncio
async def test_download_limit_streamed_bytes(tmp_path, monkeypatch):
    import royalbooks.jobs as jobs
    monkeypatch.setattr(jobs, 'MAX_BYTES', 10)
    async with httpx.AsyncClient(transport=httpx.MockTransport(lambda r: httpx.Response(200, content=b'X' * 11))) as client:
        with pytest.raises(AcquisitionError, match='file_too_large'):
            await download_epub(client, 'https://www.gutenberg.org/ebooks/42.epub', tmp_path / 'out')


@pytest.mark.asyncio
async def test_end_to_end_staging_and_handoff(tmp_path):
    store = JobStore(str(tmp_path / 'jobs.sqlite3'))
    staging = tmp_path / 'staging'
    staging.mkdir()
    ingest = tmp_path / 'cwa-ingest'
    job = store.add(42)
    epub = example_epub()
    def handler(request):
        if request.url.host == 'gutendex.com':
            return httpx.Response(200, json={'id': 42, 'copyright': False,
                'formats': {'application/epub+zip': 'https://www.gutenberg.org/ebooks/42.epub'}})
        return httpx.Response(200, content=epub)
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        await process_queued(job, store, staging, client)
    staged = store.list()[0]
    assert staged['status'] == 'staged'
    assert staged['file_size'] == len(epub)
    assert (staging / (job['id'] + '.epub')).read_bytes() == epub
    job = store.claim(('staged',))
    deliver_staged(job, store, staging, ingest)
    assert store.list()[0]['status'] == 'delivered'
    assert list(ingest.glob('*.epub'))[0].read_bytes() == epub
    assert not list(staging.glob('*.epub'))


@pytest.mark.asyncio
async def test_ineligible_copyright_rejected(tmp_path):
    store = JobStore(str(tmp_path / 'jobs.sqlite3'))
    job = store.add(7)
    staging = tmp_path / 'staging'
    staging.mkdir()
    async with httpx.AsyncClient(transport=httpx.MockTransport(lambda r: httpx.Response(200, json={
            'id': 7, 'copyright': True, 'formats': {'application/epub+zip':'https://www.gutenberg.org/ebooks/7.epub'}}))) as client:
        await process_queued(job, store, staging, client)
    assert store.list()[0]['error'] == 'source_not_eligible'
    assert store.list()[0]['status'] == 'failed'
    assert not list(staging.iterdir())


@pytest.mark.asyncio
async def test_duplicate_content_rejected(tmp_path):
    store = JobStore(str(tmp_path / 'jobs.sqlite3'))
    staging = tmp_path / 'staging'
    staging.mkdir()
    epub = example_epub()
    def handler(request):
        if request.url.host == 'gutendex.com':
            book_id = int(request.url.path.split('/')[-1])
            return httpx.Response(200, json={'id': book_id, 'copyright': False,
                 'formats': {'application/epub+zip': 'https://www.gutenberg.org/ebooks/3.epub'}})
        return httpx.Response(200, content=epub)
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        await process_queued(store.add(3), store, staging, client)
        await process_queued(store.add(4), store, staging, client)
    assert sorted(job['status'] for job in store.list()) == ['failed','staged']
    assert 'duplicate_epub' in [job['error'] for job in store.list()]


@pytest.mark.asyncio
async def test_jobs_api_auth_and_rights(monkeypatch, tmp_path):
    monkeypatch.setenv('ROYALBOOKS_API_TOKEN', 'some-long-demo-test-token')
    app.state.jobs = JobStore(str(tmp_path / 'db.sqlite3'))
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://test') as client:
        payload={'provider':'gutendex', 'source_id':42, 'rights_confirmed':True}
        assert (await client.post('/api/jobs', json=payload)).status_code == 401
        headers={'X-API-Key':'some-long-demo-test-token'}
        assert (await client.post('/api/jobs',json={**payload, 'rights_confirmed':False}, headers=headers)).status_code == 400
        assert (await client.post('/api/jobs',json={**payload, 'provider':'openlibrary'}, headers=headers)).status_code == 400
        assert (await client.post('/api/jobs',json=payload, headers=headers)).status_code == 202
        assert len((await client.get('/api/jobs', headers=headers)).json()['jobs']) == 1
        assert (await client.get('/api/jobs')).status_code == 401


@pytest.mark.asyncio
async def test_missing_token_disables_writes(monkeypatch, tmp_path):
    monkeypatch.delenv('ROYALBOOKS_API_TOKEN', raising=False)
    app.state.jobs = JobStore(str(tmp_path / 'db.sqlite3'))
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://test') as client:
        result = await client.post('/api/jobs',json={'source_id':1, 'rights_confirmed':True})
    assert result.status_code == 503
