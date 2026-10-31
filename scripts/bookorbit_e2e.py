"""Opt-in real publisher EPUB -> validation -> Book Dock, using an isolated DB."""
import argparse
import asyncio
import json
from pathlib import Path

import httpx

from royalbooks.bookorbit import Client, Config, Handoffs, phase
from royalbooks.jobs import JobStore, download_epub, validate_epub, deliver_staged

URL='https://www.gutenberg.org/cache/epub/2229/pg2229-images.epub'


async def run(args):
    data=Path(args.data_dir);data.mkdir(parents=True,exist_ok=True)
    stage=data/'staging';stage.mkdir(exist_ok=True)
    store=JobStore(str(data/'bookorbit-test.sqlite3'))
    job=store.add(2229,'gutendex',v2_authorized=True)
    if job['status']=='queued':
        target=stage/(job['id']+'.epub')
        async with httpx.AsyncClient(timeout=20,follow_redirects=False,trust_env=False) as client:
            size=await download_epub(client,URL,target,source='gutendex')
        digest=validate_epub(target)
        phase(store.path,job['id'],'download_complete');phase(store.path,job['id'],'validated')
        store.update(job['id'],'staged',sha256=digest,file_size=size)
    if store.get(job['id'])['status']=='staged':
        deliver_staged(store.get(job['id']),store,stage,Path(args.ingest))
    job=store.get(job['id']);ledger=Handoffs(store.path,args.ingest)
    report={'job_id':job['id'],'status':job['status'],'error':job['error'],
            'bytes':job['file_size'],'sha256':job['sha256'],'library_transfer':ledger.describe(job)}
    if args.verify:
        client=Client(Config.environment())
        try:report['verification']=await ledger.verify(job,client)
        finally:await client.close()
    print(json.dumps(report,ensure_ascii=False,indent=2))
    return 0 if job['status']=='delivered' else 2


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--live',action='store_true')
    parser.add_argument('--rights-confirmed',action='store_true')
    parser.add_argument('--data-dir')
    parser.add_argument('--ingest')
    parser.add_argument('--verify',action='store_true',help='Use configured BookOrbit credentials/import verification; no finalize request')
    args=parser.parse_args()
    if not args.live:
        print('DRY RUN: official Gutenberg Faust EPUB; isolated queue and Book Dock. No network or writes.');return
    if not args.rights_confirmed or not args.data_dir or not args.ingest:
        parser.error('--live requires --rights-confirmed, --data-dir and --ingest')
    raise SystemExit(asyncio.run(run(args)))


if __name__=='__main__':main()
