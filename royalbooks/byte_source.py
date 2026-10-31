"""Bounded Byte.to catalog and source-listed link-container discovery."""
from __future__ import annotations

import asyncio
import json
import re
import sqlite3
import time
from pathlib import Path
from html.parser import HTMLParser
from urllib.parse import parse_qs, urljoin, urlsplit

import httpx

from royalbooks.database import connect
from royalbooks.providers import Book, Provider, ProviderError

ORIGIN = 'https://byte.to'
MAX_HTML = 2 * 1024 * 1024
DETAIL = re.compile(r'/category/Ebooks/[A-Za-z0-9_-]+-([1-9][0-9]{0,9})\.html')


def source_url(url, kind='detail'):
    try:
        p = urlsplit(url)
        if p.scheme != 'https' or p.hostname != 'byte.to' or p.port not in (None,443) or p.username or p.password or p.fragment:
            return False
        if kind == 'detail':return not p.query and bool(DETAIL.fullmatch(p.path))
        if kind == 'widget':return p.path == '/widgets/button.php' and 1 <= len(p.query) <= 1200
        if kind == 'go':return p.path == '/go.php' and set(parse_qs(p.query)) == {'hash'} and len(p.query) <= 1500
        return p.path == '/' and len(p.query) <= 1200
    except (TypeError,ValueError):return False


def container_url(url):
    try:
        p=urlsplit(url)
        if p.scheme!='https' or p.port not in (None,443) or p.username or p.password or p.fragment:return False
        if p.hostname in {'filecrypt.cc','filecrypt.co'}:
            return bool(re.fullmatch(r'/Container/[A-Za-z0-9]+\.html',p.path)) and not p.query
        if p.hostname=='nitroflare.com':return bool(re.fullmatch(r'/view/[A-Za-z0-9]+(?:/[^/]+)?',p.path)) and not p.query
        if p.hostname=='ddownload.com':return bool(re.fullmatch(r'/[a-z0-9]{12}(?:/[^/]+)?',p.path)) and not p.query
    except (TypeError,ValueError):pass
    return False


class Page(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.links=[];self.widgets=[];self.text=[];self.anchor=None;self.ignore=0

    def handle_starttag(self,tag,attrs):
        a=dict(attrs)
        if tag in {'script','style'}:self.ignore+=1
        if tag=='a':self.anchor=[urljoin(ORIGIN,a.get('href','')),'',a]
        if tag=='iframe':
            url=urljoin(ORIGIN,a.get('src',''))
            if source_url(url,'widget') and url not in self.widgets:self.widgets.append(url)

    def handle_data(self,data):
        if not self.ignore:
            if self.anchor:self.anchor[1]+=data
            if data.strip():self.text.append(data.strip())

    def handle_endtag(self,tag):
        if tag in {'script','style'}:self.ignore=max(0,self.ignore-1)
        if tag=='a' and self.anchor:
            self.links.append(self.anchor);self.anchor=None


def parse_catalog(html):
    p=Page();p.feed(html)
    result=[];seen=set()
    for url,title,_ in p.links:
        if not source_url(url):continue
        record=int(DETAIL.fullmatch(urlsplit(url).path)[1])
        title=' '.join(title.split())[:500]
        if record in seen or not title:continue
        seen.add(record)
        parts=title.rsplit(' - ',1)
        result.append({'source_id':record,'release':title,'title':parts[0],
                       'authors':[parts[1]] if len(parts)==2 else [],'info_url':url})
    if not result and not any(s in html.casefold() for s in ['keine eintr','keine ergebnisse','nichts gefunden','keine dateien']):
        raise ProviderError('byte_catalog_unrecognized')
    return result


class ByteTo(Provider):
    name='byte';purpose='hoster_catalog';min_interval=1.5

    def __init__(self,path):
        super().__init__();self.path=path
        self.pages={}

    async def html(self,client,url,kind='detail'):
        if not source_url(url,kind):raise ProviderError('byte_untrusted_url')
        async with self._lock:
            if self.cooldown_remaining():raise ProviderError('byte_source_cooldown')
            await asyncio.sleep(max(0,self.min_interval-(time.monotonic()-self._last_request)))
            self._last_request=time.monotonic()
            async with client.stream('GET',url,follow_redirects=False) as r:
                if r.status_code!=200:
                    if r.status_code in {403,429,451}:self._blocked_until=time.monotonic()+1800
                    raise ProviderError('http_'+str(r.status_code))
                data=bytearray()
                async for chunk in r.aiter_bytes(65536):
                    data.extend(chunk)
                    if len(data)>MAX_HTML:raise ProviderError('byte_page_too_large')
        return data.decode('utf-8',errors='replace')

    def remember(self,records):
        Path(self.path).parent.mkdir(parents=True,exist_ok=True)
        with connect(self.path,timeout=5) as db:
            db.execute('CREATE TABLE IF NOT EXISTS byte_catalog(id INTEGER PRIMARY KEY,payload TEXT NOT NULL,touched INTEGER NOT NULL)')
            for item in records:
                db.execute('INSERT INTO byte_catalog VALUES(?,?,?) ON CONFLICT(id) DO UPDATE SET payload=excluded.payload,touched=excluded.touched',
                           (item['source_id'],json.dumps(item),int(time.time())))

    def lookup(self,record):
        try:
            with connect(self.path,timeout=5) as db:
                row=db.execute('SELECT payload FROM byte_catalog WHERE id=?',(record,)).fetchone()
        except sqlite3.OperationalError:
            raise ProviderError('byte_unknown_record') from None
        if not row:raise ProviderError('byte_unknown_record')
        item=json.loads(row[0])
        if not source_url(item['info_url']):raise ProviderError('byte_untrusted_url')
        return item

    async def catalog(self,client,query='',page=1):
        from urllib.parse import urlencode
        offset=(page-1)*25;upstream=offset//200+1;key=(query,upstream)
        cached=self.pages.get(key)
        if cached and cached[0]>time.monotonic():records=cached[1]
        else:
            args={'cat':43,'start':upstream}
            if query:args.update(q=query,t=1,e=1)
            records=parse_catalog(await self.html(client,ORIGIN+'/?'+urlencode(args),'catalog'))
            await asyncio.to_thread(self.remember,records)
            if len(self.pages)>64:self.pages.clear()
            self.pages[key]=(time.monotonic()+300,records)
        start=offset%200
        return records[start:start+25],len(records)>start+25 or len(records)==200

    async def search_page(self,client,query,language,limit,page=1,mode='all'):
        records,_=await self.catalog(client,query,page)
        # Release titles may contain authors; language/format are not inferred from the category.
        return [Book(source='byte',source_id=str(r['source_id']),title=r['title'],authors=r['authors'],
                     language=None,year=None,cover_url=None,info_url=r['info_url'],
                     availability='hoster_container_unverified',record_kind='release') for r in records[:limit]]

    async def search(self,client,query,language,limit):
        return await self.search_page(client,query,language,limit)

    async def resolve(self,client,record):
        item=await asyncio.to_thread(self.lookup,record)
        page=Page();page.feed(await self.html(client,item['info_url']))
        links=[]
        for widget in page.widgets[:8]:
            p=Page();p.feed(await self.html(client,widget,'widget'))
            for url,_,attrs in p.links:
                if source_url(url,'go') and 'loadbutton' in attrs.get('class','').split():
                    async with client.stream('GET',url,follow_redirects=False) as r:
                        if r.status_code not in {301,302,303,307,308}:raise ProviderError('byte_container_unresolved')
                        target=urljoin(url,r.headers.get('location',''))
                    if not container_url(target):raise ProviderError('byte_unsupported_container')
                    if target not in links:links.append(target)
        if not links:raise ProviderError('byte_no_download_containers')
        return item,links
