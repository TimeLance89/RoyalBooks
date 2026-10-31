"""Genre browsing using actual subject indexes, never title-word guesses."""
import asyncio
import time
from royalbooks.providers import OpenLibrary,Gutendex
from royalbooks.matching import normalize,normalize_person
from royalbooks.settings import PlatformSettings
from royalbooks.catalog_labels import remember

GENRES={
 'crime':('Krimi & Thriller','mystery OR subject:thriller','Detective and mystery stories','#a36e7a'),
 'fantasy':('Fantasy','fantasy','Fantasy fiction','#8778af'),
 'romance':('Romance','romance','Love stories','#b87980'),
 'science-fiction':('Science-Fiction','science_fiction','Science fiction','#608fa5'),
 'horror':('Horror','horror','Horror tales','#927780'),
 'historical':('Historische Romane','historical_fiction','Historical fiction','#a88c67'),
 'adventure':('Abenteuer','adventure','Adventure stories','#6f967c'),
 'children':('Kinder & Jugend','juvenile_fiction','Children','#9e955f'),
 'nonfiction':('Sachbücher','nonfiction','Nonfiction','#6c9a9b'),
}

def manifest():
 return [{'id':key,'title':value[0],'color':value[3]} for key,value in GENRES.items()]

class Genres:
 def __init__(self,store):
  self.store=store;self.slots=asyncio.Semaphore(2);self.pending={}
  self.openlibrary=OpenLibrary();self.gutendex=Gutendex()

 async def browse(self,slug,language,page):
  selected=(await asyncio.to_thread(PlatformSettings(self.store.path).get))['sources']
  key='genre:'+slug+':'+language+':'+str(page)+':'+','.join(sorted(selected))
  cached,updated,_=await asyncio.to_thread(self.store.get,key)
  if cached and updated and time.time()-updated<21600:return {**cached,'cached':True}
  if key not in self.pending:self.pending[key]=asyncio.create_task(self.fetch(key,slug,language,page,selected,cached))
  try:return await asyncio.shield(self.pending[key])
  finally:
   if key in self.pending and self.pending[key].done():self.pending.pop(key,None)

 async def fetch(self,key,slug,language,page,selected,cached):
  title,subject,topic,_=GENRES[slug]
  async def source(name):
   if name not in selected:return [],False,{'provider':name,'status':'disabled'}
   try:
    async with asyncio.timeout(12):
     if name=='openlibrary':
      query=('(subject:'+subject+')' if ' OR ' in subject else 'subject:"'+subject.replace('_',' ')+'"')+({'de':' AND language:ger','en':' AND language:eng'}.get(language,''))
      books=await self.openlibrary.search(self.store.client,query,None if language=='all' else language,25,page=page,edition_titles=True)
      return books,len(books)==25,{'provider':name,'status':'found' if books else 'empty'}
     params={'topic':topic,'mime_type':'application/epub+zip','copyright':'false','page':page}
     if language!='all':params['languages']=language
     data=await self.gutendex.get_json(self.store.client,'https://gutendex.com/books/',params)
     books=Gutendex.parse_books(data)
     return books,bool(data.get('next')),{'provider':name,'status':'found' if books else 'empty'}
   except Exception:return [],False,{'provider':name,'status':'error'}
  async with self.slots:
   groups=await asyncio.gather(source('openlibrary'),source('gutendex'))
  items=[book.as_dict() for books,_,_ in groups for book in books]
  # Existing EPUB editions may be attached only to a subject-indexed work
  # with the same title and author. No inference from author's other books.
  snapshot=await asyncio.to_thread(self.store.snapshot)
  files={}
  for shelf in snapshot['shelves']:
   for item in shelf['items']:
    if item.get('download_supported') and item['source'] in selected and (language=='all' or item.get('language')==language):
     files.setdefault(normalize(item['title']),[]).append(item)
  extra={}
  for item in items:
   authors={normalize_person(name) for name in item['authors']}
   for file in files.get(normalize(item['title']),[]):
    if authors&{normalize_person(name) for name in file['authors']}:
     extra[(file['source'],file['source_id'])]={**file,'genre_work':item['info_url']}
  items=list(extra.values())+items
  states=[state for _,_,state in groups]
  if not items and cached and any(s['status']=='error' for s in states):return {**cached,'stale':True}
  result={'genre':slug,'title':title,'language':language,'page':page,'items':items,'has_more':any(more for _,more,_ in groups),'sources':states}
  if items or not any(s['status']=='error' for s in states):
   await asyncio.to_thread(self.store.save,key,result)
   await asyncio.to_thread(remember,self.store.path,items)
  return result

 async def close(self):
  for task in self.pending.values():task.cancel()
  await asyncio.gather(*self.pending.values(),return_exceptions=True)
