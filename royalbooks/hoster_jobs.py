"""Persistent, idempotent folder-watch bridge to a separate download engine.

No cloud account, hoster premium account or direct-link guessing is required.
Only complete validated EPUBs in the job's own output directory are imported.
"""
from __future__ import annotations

import asyncio
import json
import os
import re
import shutil
import time
import uuid
import zipfile
from pathlib import Path
from defusedxml import ElementTree

from royalbooks.database import connect
from royalbooks.jobs import validate_epub, deliver_staged, MAX_BYTES, AcquisitionError, RETRYABLE_ERRORS
from royalbooks.providers import ProviderError
from royalbooks.byte_source import container_url
from royalbooks.matching import normalize
from royalbooks.engine_status import read_status


class HosterJobs:
    def __init__(self,path):
        self.path=path;self.root=Path(path).parent/'hoster'
        with connect(path,timeout=10) as db:
            db.execute('''CREATE TABLE IF NOT EXISTS hoster_jobs(id TEXT PRIMARY KEY,
                source TEXT NOT NULL DEFAULT 'byte',source_id INTEGER NOT NULL UNIQUE,
                status TEXT NOT NULL,error TEXT,sha256 TEXT,file_size INTEGER,
                book TEXT NOT NULL,created_at INTEGER NOT NULL,updated_at INTEGER NOT NULL)''')
            columns={r[1] for r in db.execute('PRAGMA table_info(hoster_jobs)')}
            if 'attempts' not in columns:db.execute('ALTER TABLE hoster_jobs ADD COLUMN attempts INTEGER NOT NULL DEFAULT 0')
            if 'retry_at' not in columns:db.execute('ALTER TABLE hoster_jobs ADD COLUMN retry_at INTEGER')
            if 'dispatch_sequence' not in columns:db.execute('ALTER TABLE hoster_jobs ADD COLUMN dispatch_sequence INTEGER NOT NULL DEFAULT 0')
            db.execute("UPDATE hoster_jobs SET status='needs_review',error='interrupted_handoff' WHERE status='delivering'")
            db.execute("UPDATE hoster_jobs SET status='needs_review',error='interrupted_dispatch' WHERE status='resolving'")

    def ready(self):
        return (os.getenv('ROYALBOOKS_HOSTER_ENABLED')=='1'
                and (self.root/'watch').is_dir() and (self.root/'received').is_dir())

    def add(self,book):
        if not self.ready():raise ProviderError('hoster_engine_not_configured')
        now=int(time.time())
        with connect(self.path,timeout=10) as db:
            db.execute("INSERT OR IGNORE INTO hoster_jobs(id,source_id,status,book,created_at,updated_at) VALUES(?,?,'queued',?,?,?)",
                (uuid.uuid4().hex,book['source_id'],json.dumps(book),now,now))
            db.row_factory=__import__('sqlite3').Row
            row=dict(db.execute('SELECT * FROM hoster_jobs WHERE source_id=?',(book['source_id'],)).fetchone())
        row['book']=json.loads(row['book']);row['kind']='hoster'
        return row

    def list(self):
        with connect(self.path,timeout=10) as db:
            db.row_factory=__import__('sqlite3').Row
            rows=[dict(row) for row in db.execute('SELECT * FROM hoster_jobs ORDER BY created_at DESC LIMIT 100')]
        for row in rows:
            row['book']=json.loads(row['book']);row['kind']='hoster'
        return rows

    def update(self,job_id,status,*,error=None,sha256=None,file_size=None):
        with connect(self.path,timeout=10) as db:
            db.execute('UPDATE hoster_jobs SET status=?,error=?,sha256=COALESCE(?,sha256),file_size=COALESCE(?,file_size),updated_at=? WHERE id=?',
                (status,error,sha256,file_size,int(time.time()),job_id))

    def claim(self):
        with connect(self.path,timeout=10) as db:
            db.row_factory=__import__('sqlite3').Row
            db.execute('BEGIN IMMEDIATE')
            row=db.execute("SELECT * FROM hoster_jobs WHERE status='queued' AND attempts<3 AND (retry_at IS NULL OR retry_at<=?) ORDER BY created_at LIMIT 1",(int(time.time()),)).fetchone()
            if not row:return None
            db.execute("UPDATE hoster_jobs SET status='resolving',attempts=attempts+1,retry_at=NULL,updated_at=? WHERE id=?",(int(time.time()),row['id']))
            result=dict(db.execute('SELECT * FROM hoster_jobs WHERE id=?',(row['id'],)).fetchone())
        result['book']=json.loads(result['book']);result['kind']='hoster'
        return result

    def resolution_failed(self,job,code):
        if code in RETRYABLE_ERRORS and job['attempts']<3:
            delay=60 if job['attempts']==1 else 300
            with connect(self.path,timeout=10) as db:
                db.execute("UPDATE hoster_jobs SET status='queued',error=?,retry_at=?,updated_at=? WHERE id=? AND status='resolving'",
                           (code,int(time.time())+delay,int(time.time()),job['id']))
        else:self.update(job['id'],'failed',error=code)

    def dispatch(self,job,links):
        if not re.fullmatch(r'[a-f0-9]{32}',job['id']) or not 1<=len(links)<=8 or not all(container_url(u) for u in links):
            raise ProviderError('hoster_untrusted_container')
        watch=self.root/'watch';received=self.root/'received'/job['id']
        if watch.is_symlink() or received.is_symlink():raise ProviderError('hoster_symlink_forbidden')
        received.mkdir(parents=True,exist_ok=True)
        name=job['id']+(('-manual'+str(job['dispatch_sequence'])) if job.get('dispatch_sequence') else '')
        target=watch/(name+'.crawljob')
        if target.exists() or (watch/'added'/target.name).exists():
            self.update(job['id'],'dispatched');return
        payload=[{'text':'\n'.join(links),'packageName':'RoyalBooks-'+job['id'],
            'downloadFolder':'/downloads/'+job['id'],'autoConfirm':'TRUE','autoStart':'TRUE',
            'forcedStart':'FALSE','enabled':'TRUE','chunks':1,'deepAnalyseEnabled':False,
            'extractAfterDownload':'TRUE','overwritePackagizerEnabled':True}]
        temp=watch/(name+'.tmp')
        with temp.open('w',encoding='utf-8') as f:
            json.dump(payload,f);f.flush();os.fsync(f.fileno())
        os.replace(temp,target)
        self.update(job['id'],'dispatched')

    def manual_links(self,job_id,links):
        if not self.ready():raise ProviderError('hoster_engine_not_configured')
        if not 1<=len(links)<=8 or not all(container_url(u) and 'filecrypt.' not in u for u in links):
            raise ProviderError('hoster_direct_file_links_required')
        with connect(self.path,timeout=10) as db:
            db.row_factory=__import__('sqlite3').Row
            db.execute('BEGIN IMMEDIATE')
            row=db.execute('SELECT * FROM hoster_jobs WHERE id=?',(job_id,)).fetchone()
            if not row:raise ProviderError('hoster_job_not_found')
            if row['status'] not in {'failed','needs_review'} or row['error'] not in {
                'engine_cloudflare_blocked','engine_download_failed','byte_no_download_containers',
                'byte_unsupported_container','byte_container_unresolved','http_403','http_451',
            }:raise ProviderError('hoster_job_cannot_resume')
            db.execute("UPDATE hoster_jobs SET status='resolving',dispatch_sequence=dispatch_sequence+1,error=NULL,updated_at=? WHERE id=?",(int(time.time()),job_id))
            job=dict(db.execute('SELECT * FROM hoster_jobs WHERE id=?',(job_id,)).fetchone())
        job['book']=json.loads(job['book'])
        self.dispatch(job,list(dict.fromkeys(links)))
        return job_id

    def stage_received(self,job):
        folder=self.root/'received'/job['id']
        if not folder.is_dir() or folder.is_symlink():return
        paths=[]
        # A partial download/extraction is never accepted as a complete result.
        for index,p in enumerate(folder.rglob('*')):
            if index>=500:raise AcquisitionError('hoster_too_many_files')
            if p.is_symlink():raise AcquisitionError('hoster_symlink_forbidden')
            if p.is_file():
                if p.name.lower().endswith(('.part','.tmp','.part.rar','.part.zip')):return
                if p.suffix.lower()=='.epub':paths.append(p)
        if not paths:return
        # A settling window and complete ZIP/CRC validation avoid partial extraction.
        if any(time.time()-p.stat().st_mtime<30 for p in paths):return
        unique={}
        for p in paths:
            if p.stat().st_size>MAX_BYTES:raise AcquisitionError('file_too_large')
            digest=validate_epub(p);unique.setdefault(digest,p)
        if len(unique)!=1:raise AcquisitionError('hoster_multiple_books_need_selection')
        digest,source=next(iter(unique.items()))
        with zipfile.ZipFile(source) as archive:
            root=ElementTree.fromstring(archive.read('META-INF/container.xml'))
            opf=next(r.attrib['full-path'] for r in root.findall('.//{*}rootfile')
                     if r.attrib.get('media-type')=='application/oebps-package+xml')
            if archive.getinfo(opf).file_size>1024*1024:raise AcquisitionError('oversized_epub_metadata')
            package=ElementTree.fromstring(archive.read(opf))
            titles=[n.text or '' for n in package.findall('.//{http://purl.org/dc/elements/1.1/}title')]
        wanted=set(normalize(job['book']['title']).split())
        if not wanted or not any(len(wanted & set(normalize(title).split()))>=max(1,len(wanted)*.5) for title in titles):
            raise AcquisitionError('hoster_book_title_mismatch')
        with connect(self.path,timeout=10) as db:
            for table in ['jobs','hoster_jobs']:
                if not db.execute('SELECT 1 FROM sqlite_master WHERE name=?',(table,)).fetchone():continue
                if db.execute(f"SELECT 1 FROM {table} WHERE sha256=? AND id!=? AND status IN ('staged','delivering','delivered','needs_review')",(digest,job['id'])).fetchone():
                    raise AcquisitionError('duplicate_epub')
        staging=Path(self.path).parent/'staging';staging.mkdir(exist_ok=True)
        temp=staging/(job['id']+'.part');target=staging/(job['id']+'.epub')
        shutil.copyfile(source,temp)
        if validate_epub(temp)!=digest:temp.unlink(missing_ok=True);raise AcquisitionError('hoster_file_changed')
        os.replace(temp,target)
        self.update(job['id'],'staged',sha256=digest,file_size=target.stat().st_size)
        return True


async def worker(store,provider,client,ingest):
    while True:
        if store.ready() and os.getenv('ROYALBOOKS_DOWNLOADS_ENABLED','1')!='0':
            job=None if getattr(provider,'cooldown_remaining',lambda:0)() else await asyncio.to_thread(store.claim)
            if job:
                try:
                    _,links=await provider.resolve(client,job['source_id'])
                    await asyncio.to_thread(store.dispatch,job,links)
                except Exception as exc:
                    code=str(exc) if isinstance(exc,ProviderError) else type(exc).__name__
                    await asyncio.to_thread(store.resolution_failed,job,code[:80])
            for job in await asyncio.to_thread(store.list):
                try:
                    if job['status']=='dispatched':
                        staged=await asyncio.to_thread(store.stage_received,job)
                        if not staged:
                            snapshot=await asyncio.to_thread(read_status,store.root)
                            status=snapshot.get('jobs',{}).get(job['id'],{}).get('status')
                            if status in {'blocked','failed'}:
                                await asyncio.to_thread(store.update,job['id'],'needs_review',error='engine_cloudflare_blocked' if status=='blocked' else 'engine_download_failed')
                    if job['status']=='staged' and ingest:
                        await asyncio.to_thread(store.update,job['id'],'delivering')
                        await asyncio.to_thread(deliver_staged,job,store,Path(store.path).parent/'staging',ingest)
                except Exception as exc:
                    code=str(exc) if isinstance(exc,AcquisitionError) else type(exc).__name__
                    await asyncio.to_thread(store.update,job['id'],'needs_review',error=code[:80])
        await asyncio.sleep(3)
