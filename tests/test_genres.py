"""Subject-indexed browsing, exact edition attachment, cache and source isolation."""
import httpx
import pytest
from royalbooks.genres import Genres,GENRES
from royalbooks.storefront import Storefront
from royalbooks.settings import PlatformSettings
from royalbooks.providers import Book


def work(title='Nebelwald',author='Ada Beispiel',key='OL123W'):
 return {'key':'/works/'+key,'title':title,'author_name':[author],'language':['ger','eng'],'cover_i':123}


def file(title='Nebelwald',author='Ada Beispiel',record=123):
 return Book(source='libgen',source_id=f'{record:032x}',title=title,authors=[author],language='de',year=2024,cover_url=None,info_url='https://libgen.li/',formats=('EPUB',),acquisition_id=record).as_dict()

@pytest.mark.asyncio
async def test_genres_use_subject_filters_paging_and_never_follow_next_urls(tmp_path):
 seen=[]
 def handler(req):
  seen.append(req)
  if req.url.host=='openlibrary.org':
   assert req.url.params['q']=='subject:"fantasy" AND language:ger'
   assert req.url.params['offset']=='25'
   assert req.url.params['lang']=='de'
   assert 'editions.title' in req.url.params['fields']
   return httpx.Response(200,json={'docs':[{**work(title='Original title'),'editions':{'docs':[{'title':'Nebelwald'}]}}]})
  assert req.url.host=='gutendex.com'
  assert req.url.params['topic']=='Fantasy fiction' and req.url.params['page']=='2'
  assert req.url.params['languages']=='de' and req.url.params['copyright']=='false'
  return httpx.Response(200,json={'results':[{'id':12,'title':'Ein Märchen','authors':[{'name':'Autor'}],'languages':['de'],'copyright':False,'formats':{'application/epub+zip':'https://www.gutenberg.org/ebooks/12.epub'}}],'next':'http://127.0.0.1/private'})
 async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
  store=Storefront(str(tmp_path/'db.sqlite3'),client);genres=Genres(store)
  result=await genres.browse('fantasy','de',2)
  assert len(seen)==2 and result['has_more']
  assert len(result['items'])==2 and result['items'][1]['download_supported']
  assert result['items'][0]['language'] is None and not result['items'][0]['download_supported']
  assert (await Genres(Storefront(store.path,client)).browse('fantasy','de',2))['cached']
  assert len(seen)==2

@pytest.mark.asyncio
async def test_matching_files_require_exact_title_and_author_and_selected_language(tmp_path):
 def handler(req):return httpx.Response(200,json={'docs':[work()]} if req.url.host=='openlibrary.org' else {'results':[]})
 async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
  store=Storefront(str(tmp_path/'db.sqlite3'),client)
  wrong_language={**file(record=126),'language':'en'}
  store.save('recent',[file(),file(author='Andere Person',record=124),file(title='Anderer Roman',record=125),wrong_language])
  result=await Genres(store).browse('fantasy','de',1)
  candidates=[b for b in result['items'] if b['download_supported']]
  assert len(candidates)==1 and candidates[0]['acquisition_id']==123
  assert candidates[0]['genre_work']=='https://openlibrary.org/works/OL123W'

@pytest.mark.asyncio
async def test_disabled_genre_sources_are_not_called_and_cache_is_not_reused(tmp_path):
 calls=[]
 def handler(req):calls.append(req);return httpx.Response(200,json={'docs':[work()],'results':[]})
 async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
  store=Storefront(str(tmp_path/'db.sqlite3'),client);genres=Genres(store)
  await genres.browse('fantasy','de',1);assert len(calls)==2
  PlatformSettings(store.path).save({'sources':['libgen']})
  result=await genres.browse('fantasy','de',1)
  assert not result['items'] and all(s['status']=='disabled' for s in result['sources'])
  assert len(calls)==2

@pytest.mark.asyncio
async def test_unknown_genre_and_invalid_page_are_rejected_by_api():
 from royalbooks.app import app
 async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),base_url='http://test') as client:
  manifest=(await client.get('/api/genres')).json()['genres']
  assert len(manifest)==len(GENRES)
  assert (await client.get('/api/genres/not-a-genre')).status_code==404
  assert (await client.get('/api/genres/fantasy?page=0')).status_code==422
  assert (await client.get('/api/genres/fantasy?language=xx')).status_code==422
