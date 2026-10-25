"""Persistent store shelves: background-only refresh, last-good snapshots, real trends."""
import asyncio
import json
import re
import time
from datetime import datetime, timezone
from html import unescape

from royalbooks.builtin_search import LibraryGenesis
from royalbooks.database import connect
from royalbooks.matching import normalize, normalize_person
from royalbooks.providers import Book, ProviderError, canonical_isbn
from royalbooks.catalog_labels import remember
from royalbooks.settings import PlatformSettings

SHELVES={
    'releases':('Aktuelle Ausgaben '+str(datetime.now(timezone.utc).year),'Deutschsprachige EPUB-Ausgaben mit aktuellem Erscheinungsjahr laut Katalog.','https://libgen.li/','lf'),
    'recent':('Neu im EPUB-Katalog','Zuletzt hinzugefügte deutsche EPUBs. Das Uploaddatum ist kein Erscheinungsdatum.','https://libgen.li/','lf'),
    'fiction':('Romane & Geschichten','Neue Einträge aus dem deutschsprachigen Belletristik-Katalog.','https://libgen.li/','f'),
    'nonfiction':('Wissen & neue Perspektiven','Sachbücher zum Weiterdenken aus dem EPUB-Katalog.','https://libgen.li/','l'),
    'trending':('Angesagt weltweit','Tagestrends von Open Library. Internationale Werke; EPUB-Ausgaben werden bei der Suche geprüft.','https://openlibrary.org/trending/daily',None),
}


def unique_books(books,limit=18):
    output=[];seen=set()
    for book in books:
        # Store curation only: raw search results are retained unchanged.
        if not book.authors or re.search(r'(?i)(?:omdaru|\b3epub|\bZF_)',book.title):continue
        key=(normalize(book.title),normalize_person(book.authors[0]) if book.authors else '',book.language)
        if key in seen:continue
        seen.add(key);output.append(book.as_dict())
        if len(output)>=limit:break
    return output


def parse_trending(data):
    rows=data.get('works') if isinstance(data,dict) else None
    if not isinstance(rows,list):raise ProviderError('trending_invalid')
    books=[]
    for row in rows[:40]:
        if not isinstance(row,dict):continue
        key=row.get('key');title=row.get('title');cover=row.get('cover_i')
        if not isinstance(key,str) or not re.fullmatch(r'/works/OL[0-9]{1,12}W',key) or not isinstance(title,str):continue
        authors=row.get('author_name',[])
        year=row.get('first_publish_year')
        books.append(Book(source='openlibrary',source_id=key.rsplit('/',1)[1],title=title[:350],
            authors=[a[:150] for a in authors[:8] if isinstance(a,str)] if isinstance(authors,list) else [],
            language=None,year=year if type(year) is int else None,
            cover_url=f'https://covers.openlibrary.org/b/id/{cover}-L.jpg?default=false' if type(cover) is int and cover>0 else None,
            info_url='https://openlibrary.org'+key,record_kind='work'))
    return unique_books(books)


class Storefront:
    def __init__(self,path,client):
        self.path=path;self.client=client;self.refreshing=False
        self.availability=None
        self.libgen=LibraryGenesis(path);self.preview_slots=asyncio.Semaphore(2)
        with connect(path,timeout=5) as db:
            db.execute('CREATE TABLE IF NOT EXISTS store_snapshots (name TEXT PRIMARY KEY,payload TEXT NOT NULL,updated_at INTEGER NOT NULL,last_error TEXT)')

    def get(self,name):
        with connect(self.path,timeout=5) as db:
            row=db.execute('SELECT payload,updated_at,last_error FROM store_snapshots WHERE name=?',(name,)).fetchone()
        return (json.loads(row[0]),row[1],row[2]) if row else (None,None,None)

    def save(self,name,payload):
        if isinstance(payload,list):remember(self.path,payload)
        with connect(self.path,timeout=5) as db:
            db.execute('INSERT INTO store_snapshots VALUES(?,?,?,NULL) ON CONFLICT(name) DO UPDATE SET payload=excluded.payload,updated_at=excluded.updated_at,last_error=NULL',(name,json.dumps(payload,ensure_ascii=False),int(time.time())))

    def failed(self,name,error):
        code=str(error) if isinstance(error,ProviderError) and re.fullmatch(r'[A-Za-z0-9_]{1,80}',str(error)) else type(error).__name__
        with connect(self.path,timeout=5) as db:
            db.execute("INSERT INTO store_snapshots VALUES(?,'[]',0,?) ON CONFLICT(name) DO UPDATE SET last_error=excluded.last_error",(name,code))

    def snapshot(self):
        shelves=[]
        for name,(title,note,url,_) in SHELVES.items():
            items,updated,error=self.get(name)
            shelves.append({'id':name,'title':title,'note':note,'source_url':url,'items':items or [],
                           'updated_at':updated or None,'stale':bool(error or not updated or time.time()-updated>7200),
                           'status':'cached' if error and items else 'unavailable' if error else 'ready' if items else 'loading'})
        return {'shelves':shelves,'refreshing':self.refreshing}

    async def refresh_one(self,name):
        try:
            selected=(await asyncio.to_thread(PlatformSettings(self.path).get))['sources']
            if ('openlibrary' if name=='trending' else 'libgen') not in selected:return
            async with asyncio.timeout(20):
                if name=='trending':
                    r=await self.client.get('https://openlibrary.org/trending/daily.json',params={'limit':24})
                    if r.status_code!=200:raise ProviderError('trending_http_'+str(r.status_code))
                    items=parse_trending(r.json())
                else:
                    kwargs={'year':datetime.now(timezone.utc).year} if name=='releases' else {}
                    books=await self.libgen.latest(self.client,'de',25,topics=SHELVES[name][3],**kwargs)
                    items=unique_books(books)
                if not items:raise ProviderError('store_shelf_empty')
                await asyncio.to_thread(self.save,name,items)
                if self.availability:
                    records=[(item['source'],int(item.get('acquisition_id') or item['source_id']))
                             for item in items if item.get('download_supported')]
                    await self.availability.request(records,priority=5)
        except Exception as error:
            await asyncio.to_thread(self.failed,name,error)

    async def refresh(self):
        if self.refreshing:return
        self.refreshing=True
        try:
            # One LibGen instance serializes catalog operations; independent
            # trends can finish and be displayed while catalog shelves load.
            await asyncio.gather(*(self.refresh_one(name) for name in SHELVES))
        finally:self.refreshing=False

    async def run(self):
        while True:
            await self.refresh()
            await asyncio.sleep(3600)

    async def preview(self,isbn=None,work=None):
        key='preview:'+(isbn or work)
        cached,updated,error=await asyncio.to_thread(self.get,key)
        if updated and time.time()-updated<7*86400:return {**cached,'cached':True}
        async with self.preview_slots:
            try:
                async with asyncio.timeout(6):
                    url='https://openlibrary.org/isbn/'+isbn+'.json' if isbn else 'https://openlibrary.org/works/'+work+'.json'
                    r=await self.client.get(url)
                    if r.status_code!=200:raise ProviderError('preview_unavailable')
                    data=r.json()
                    description=data.get('description')
                    scope='edition' if isbn else 'work'
                    if isbn and not description:
                        works=data.get('works',[])
                        target=works[0].get('key') if works and isinstance(works[0],dict) else None
                        if isinstance(target,str) and re.fullmatch(r'/works/OL[0-9]{1,12}W',target):
                            r=await self.client.get('https://openlibrary.org'+target+'.json')
                            if r.status_code==200:
                                data=r.json();description=data.get('description');scope='work'
                    if isinstance(description,dict):description=description.get('value')
                    description=unescape(re.sub(r'<[^>]*>',' ',description))[:12000] if isinstance(description,str) else None
                    result={'description':description,'source':'Open Library','source_url':url.removesuffix('.json'),
                            'scope':scope}
                    await asyncio.to_thread(self.save,key,result)
                    return result
            except Exception:
                if isinstance(cached,dict):return {**cached,'stale':True}
                return {'description':None,'source':'Open Library','unavailable':True}
