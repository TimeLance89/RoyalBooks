"""Bounded, cached reachability probes. A ZIP prefix is not full EPUB validation."""
import asyncio
import itertools
import re
import time
from urllib.parse import urlsplit,parse_qs,urljoin
import httpx
from royalbooks.database import connect
from royalbooks.direct_sources import SOURCES,lookup_epub,safe_epub_url
from royalbooks.libgen_links import resolve_epub,safe_file_url
from royalbooks.jobs import MAX_BYTES,valid_url

class Availability:
    def __init__(self,path):
        self.path=path;self.pending=set();self.queue=asyncio.PriorityQueue(maxsize=160)
        self.serial=itertools.count();self.tasks=[];self.client=None
        with connect(path,timeout=5) as db:
            db.execute('CREATE TABLE IF NOT EXISTS file_availability (source TEXT NOT NULL,record_id INTEGER NOT NULL,status TEXT NOT NULL,checked_at INTEGER NOT NULL,error TEXT,size INTEGER,PRIMARY KEY(source,record_id))')

    def read(self,source,record_id):
        with connect(self.path,timeout=5) as db:row=db.execute('SELECT status,checked_at,error,size FROM file_availability WHERE source=? AND record_id=?',(source,record_id)).fetchone()
        return dict(source=source,record_id=record_id,status=row[0],checked_at=row[1],error=row[2],size=row[3]) if row else dict(source=source,record_id=record_id,status='pending',checked_at=0,error=None,size=None)

    def save(self,source,record_id,status,error=None,size=None):
        with connect(self.path,timeout=5) as db:
            db.execute('INSERT INTO file_availability VALUES(?,?,?,?,?,?) ON CONFLICT(source,record_id) DO UPDATE SET status=excluded.status,checked_at=excluded.checked_at,error=excluded.error,size=excluded.size',(source,record_id,status,int(time.time()),error,size))
            db.execute('DELETE FROM file_availability WHERE checked_at<?',(int(time.time())-14*86400,))

    async def request(self,records,priority=0):
        output=[]
        for source,record_id in dict.fromkeys(records):
            item=await asyncio.to_thread(self.read,source,record_id)
            ttl=900 if item['status']=='available' else 120
            if item['status'] in {'available','unavailable'} and time.time()-item['checked_at']<ttl:
                output.append(item);continue
            key=(source,record_id)
            if key not in self.pending:
                if self.queue.full():
                    output.append({**item,'status':'busy'});continue
                self.pending.add(key);self.queue.put_nowait((priority,next(self.serial),key))
            output.append({**item,'status':'pending'})
        return output

    async def start(self):
        self.client=httpx.AsyncClient(timeout=httpx.Timeout(8,connect=4),trust_env=False,follow_redirects=False,
            limits=httpx.Limits(max_connections=6),headers={'User-Agent':'RoyalBooks/2.0 (file availability check)'})
        self.tasks=[asyncio.create_task(self.worker()) for _ in range(3)]

    async def close(self):
        for task in self.tasks:task.cancel()
        await asyncio.gather(*self.tasks,return_exceptions=True)
        if self.client:await self.client.aclose()

    async def worker(self):
        while True:
            _,_,key=await self.queue.get()
            try:
                async with asyncio.timeout(18):
                    size=await self.probe(*key)
                await asyncio.to_thread(self.save,*key,'available',size=size)
            except asyncio.CancelledError:raise
            except Exception as error:
                code=str(error) if re.fullmatch(r'[A-Za-z0-9_]{1,80}',str(error)) else type(error).__name__
                await asyncio.to_thread(self.save,*key,'unavailable',error=code)
            finally:
                self.pending.discard(key);self.queue.task_done()

    async def probe(self,source,record_id):
        if source=='webshare':
            from royalbooks.webshare_source import cached
            hit=await asyncio.to_thread(cached,self.path,record_id)
            if not hit:raise ValueError('verified_epub_cache_expired_search_again')
            return len(hit[0])
        headers={'Range':'bytes=0-1023'};wanted=None
        if source in SOURCES:
            url=await asyncio.to_thread(lookup_epub,self.path,source,record_id)
            checker=lambda target:safe_epub_url(source,target)
            if source=='libgen' and url:
                wanted=parse_qs(urlsplit(url).query)['md5'][0].lower()
                headers['Referer']=url
                url=await resolve_epub(self.client,url)
                checker=lambda target:(safe_file_url(target) and parse_qs(urlsplit(target).query).get('md5',[''])[0].lower()==wanted)
        elif source=='gutenberg_catalog':
            from royalbooks.gutenberg_formats import lookup_epub as official
            url=await asyncio.to_thread(official,self.path,record_id);checker=valid_url
        elif source=='gutendex':
            r=await self.client.get(f'https://gutendex.com/books/{record_id}/')
            if r.status_code!=200:raise ValueError('catalog_unavailable')
            data=r.json()
            if data.get('id')!=record_id or data.get('copyright') is not False:raise ValueError('source_not_eligible')
            url=data.get('formats',{}).get('application/epub+zip');checker=valid_url
        else:raise ValueError('not_file_source')
        if not url or not checker(url):raise ValueError('source_metadata_expired')
        for _ in range(4):
            async with self.client.stream('GET',url,headers=headers,follow_redirects=False) as r:
                if r.status_code in {301,302,303,307,308}:
                    target=urljoin(url,r.headers.get('location',''))
                    if not checker(target):raise ValueError('untrusted_redirect')
                    url=target;continue
                if r.status_code not in {200,206}:raise ValueError('http_'+str(r.status_code))
                total=r.headers.get('content-range','').rsplit('/',1)[-1] if r.status_code==206 else r.headers.get('content-length')
                size=int(total) if total and total.isdigit() else None
                if size and size>MAX_BYTES:raise ValueError('file_too_large')
                if r.status_code==206 and not r.headers.get('content-range','').startswith('bytes 0-'):
                    raise ValueError('invalid_content_range')
                prefix=bytearray()
                async for chunk in r.aiter_bytes(64):
                    prefix.extend(chunk)
                    if len(prefix)>=4:break
                if not prefix.startswith(b'PK\x03\x04'):raise ValueError('not_epub_zip')
                return size
        raise ValueError('too_many_redirects')
