"""Reproducible live evidence; no network or book download without explicit flags."""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import tempfile
from datetime import datetime, timezone
from pathlib import Path

import httpx

from royalbooks.discovery import SearchCoordinator, bounded_response
from royalbooks.source_registry import configured_providers
from royalbooks.jobs import JobStore, process_queued

CASES = [('Faust', 'de'), ('Don Kichote', 'de'), ('Kafka', 'de'), ('Suspense', 'en')]


async def audit(*, download: bool = False) -> dict:
    report = {'tested_at': datetime.now(timezone.utc).isoformat(), 'cases': [],
              'notice': 'Catalog responses are not file verification or copyright clearance.',
              'download': {'status': 'not_requested'}, 'downloads': []}
    # Isolated DB: neither production jobs nor the Calibre library are touched.
    with tempfile.TemporaryDirectory(prefix='royalbooks-audit-') as directory:
        path = str(Path(directory)/'audit.sqlite3')
        previous = os.environ.get('ROYALBOOKS_DB_PATH')
        os.environ['ROYALBOOKS_DB_PATH'] = path
        try:
            providers = configured_providers()
        finally:
            if previous is None:
                os.environ.pop('ROYALBOOKS_DB_PATH', None)
            else:
                os.environ['ROYALBOOKS_DB_PATH'] = previous
        async with httpx.AsyncClient(timeout=5, follow_redirects=False, trust_env=False,
                                     event_hooks={'response': [bounded_response]},
                                     headers={'User-Agent': 'RoyalBooks/2.0 source audit'}) as client:
            engine = SearchCoordinator(providers, client)
            candidates = []
            for query, language in CASES:
                books, states, _ = await engine.search(query, language, 10)
                report['cases'].append({'query': query, 'language': language, 'sources': states,
                                        'results': [{'source': b.source, 'id': b.source_id,
                                                     'language': b.language, 'isbn': b.isbn13,
                                                     'listed_formats': b.as_dict()['formats'],
                                                     'epub_listed': bool(b.epub_url)} for b in books]})
                # 1648 German Cervantes translation, publisher-listed export.
                if query == 'Don Kichote':
                    candidate = next((b for b in books if b.source == 'wikisource' and b.epub_url), None)
                    if candidate:
                        candidates.append(candidate)
                if query == 'Suspense':
                    candidate = next((b for b in books if b.source == 'standardebooks' and b.epub_url), None)
                    if candidate:
                        candidates.append(candidate)
            await engine.close()
        if download:
            if not candidates:
                report['download'] = {'status': 'unavailable', 'source': 'wikisource'}
            for candidate in candidates:
                store = JobStore(path)
                job = store.add(int(candidate.source_id), candidate.source, v2_authorized=True)
                staging = Path(directory)/'staging'
                staging.mkdir(exist_ok=True)
                async with httpx.AsyncClient(timeout=30, follow_redirects=False, trust_env=False) as client:
                    try:
                        await asyncio.wait_for(process_queued(job, store, staging, client), timeout=90)
                    except TimeoutError:
                        store.update(job['id'], 'failed', error='ReadTimeout')
                actual = store.get(job['id'])
                evidence = {k: actual[k] for k in ['source', 'status', 'error', 'sha256', 'file_size']}
                evidence['calibre_handoff'] = 'not_attempted'
                report['downloads'].append(evidence)
            report['download'] = {'attempted': len(report['downloads']),
                                  'validated': sum(d['status'] == 'staged' for d in report['downloads'])}
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--live', action='store_true')
    parser.add_argument('--download-publisher-epub', action='store_true')
    parser.add_argument('--output', type=Path, default=Path('data/source-audit.json'))
    args = parser.parse_args()
    if not args.live:
        print('Dry run. Use --live for catalog checks; add --download-publisher-epub for file validation.')
        return
    report = asyncio.run(audit(download=args.download_publisher_epub))
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, indent=2) + '\n', encoding='utf-8')
    print(json.dumps({'output': str(args.output), 'download': report['download']}))
    if not any(c['results'] for c in report['cases']):
        raise SystemExit('No real catalog results: integration check failed')


if __name__ == '__main__':
    main()
