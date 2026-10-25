"""Independent public Memory of the World JSON catalog and source-listed EPUBs."""
import asyncio
import re
from html import unescape

from royalbooks.providers import Book, Provider, ProviderError, canonical_isbn
from royalbooks.direct_sources import index_epub
from royalbooks.memory_links import ORIGIN, listed_url

LANGUAGES={'deu':'de','ger':'de','de':'de','eng':'en','en':'en'}


class MemoryOfTheWorld(Provider):
    name='memoryoftheworld'
    purpose='file_listing'
    min_interval=1.2

    def __init__(self,db_path):
        super().__init__()
        self.db_path=db_path

    async def search(self,client,query,language,limit):
        output=[];seen=set()
        # Preserve the user's query on every bounded page; do not follow arbitrary
        # _links or accept remote file origins from catalog metadata.
        for offset in (0,48,96):
            data=await self.get_json(client,ORIGIN+'/search',{'q':query,'limit':48,'offset':offset})
            rows=data.get('_items')
            if not isinstance(rows,list):
                raise ProviderError('memory_catalog_invalid')
            for row in rows[:48]:
                if not isinstance(row,dict):continue
                external=row.get('_id');title=row.get('title')
                if not isinstance(external,str) or not re.fullmatch(r'[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}',external) or not isinstance(title,str) or not 1<=len(title)<=350:
                    continue
                if external in seen:continue
                langs=row.get('languages',[])
                if not isinstance(langs,list):continue
                normalized={LANGUAGES.get(v) for v in langs if isinstance(v,str)}
                if language and (language not in normalized or normalized!={language}):continue
                lang=next(iter(normalized)) if len(normalized)==1 else None
                formats=row.get('formats',[])
                if not isinstance(formats,list):continue
                url=None
                for fmt in formats:
                    if not isinstance(fmt,dict) or str(fmt.get('format','')).casefold()!='epub':continue
                    url=listed_url(row.get('library_url'),fmt.get('dir_path'),fmt.get('file_name'))
                    if url:break
                if not url:continue
                seen.add(external)
                record_id=await asyncio.to_thread(index_epub,self.db_path,self.name,external,url)
                authors=row.get('authors',[])
                if not isinstance(authors,list):authors=[]
                isbn=[]
                for entry in row.get('identifiers',[]) if isinstance(row.get('identifiers'),list) else []:
                    if isinstance(entry,dict) and entry.get('scheme')=='isbn':
                        value=canonical_isbn(str(entry.get('code','')))
                        if value:isbn.append(value)
                year=row.get('pubdate','')
                description=row.get('abstract','')
                output.append(Book(source=self.name,source_id=str(record_id),title=title,
                    authors=[a[:150] for a in authors[:8] if isinstance(a,str)],language=lang,
                    year=int(year[:4]) if isinstance(year,str) and re.match(r'^[12]\d{3}',year) else None,
                    cover_url=self._cover(row),
                    info_url=ORIGIN+'/#/book/'+external,epub_url=url,formats=('EPUB',),
                    isbns=tuple(isbn),acquisition_id=record_id,
                    availability='catalog_listing_unverified',language_evidence='catalog_claim_unverified',
                    description=unescape(re.sub(r'<[^>]*>',' ',description))[:6000] if isinstance(description,str) else None))
                if len(output)>=limit:return output
            if len(rows)<48:break
            meta=data.get('_meta',{})
            if isinstance(meta,dict) and type(meta.get('total')) is int and meta['total']<=offset+len(rows):break
        return output

    @staticmethod
    def _cover(row):
        path=row.get('cover_url')
        if not isinstance(path,str):return None
        directory,name=path.rsplit('/',1) if '/' in path else ('',path)
        return listed_url(row.get('library_url'),directory,name,'jpg')
