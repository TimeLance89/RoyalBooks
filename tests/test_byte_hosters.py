import asyncio
import io
import json
import os
import time
import zipfile

import httpx
import pytest

from royalbooks.byte_source import ByteTo,parse_catalog,source_url,container_url
from royalbooks.hoster_jobs import HosterJobs,worker
from royalbooks.jobs import AcquisitionError
from royalbooks.providers import ProviderError
from royalbooks.engine_status import summarize,read_status

URL='https://byte.to/category/Ebooks/Faust-Goethe-12345.html'
ITEM={'source_id':12345,'title':'Faust','authors':['Goethe'],'info_url':URL,'release':'Faust - Goethe'}

def catalog(count=1):
    return ''.join(f'<p class="TITLE"><a " href="/category/Ebooks/Faust-Goethe-{12345+i}.html">Faust - Goethe</a></p>' for i in range(count))

def epub(title='Faust'):
    out=io.BytesIO()
    with zipfile.ZipFile(out,'w') as z:
        z.writestr('mimetype','application/epub+zip')
        z.writestr('META-INF/container.xml','<container><rootfile full-path="book.opf" media-type="application/oebps-package+xml"/></container>')
        z.writestr('book.opf',f'<package xmlns:dc="http://purl.org/dc/elements/1.1/"><metadata><dc:title>{title}</dc:title></metadata></package>')
    return out.getvalue()

def engine(tmp_path,monkeypatch):
    monkeypatch.setenv('ROYALBOOKS_HOSTER_ENABLED','1')
    store=HosterJobs(str(tmp_path/'db.sqlite3'))
    for p in ['watch','received']:(store.root/p).mkdir(parents=True)
    return store

def test_catalog_ignores_ads_other_categories_and_duplicates():
    html=catalog()+catalog()+'<a href="/category/Movies/Film-22.html">Other</a><a href="https://evil.org/category/Ebooks/Test-12.html">Wrong</a>'
    assert parse_catalog(html)==[ITEM]
    with pytest.raises(ProviderError,match='unrecognized'):parse_catalog('<title>Just a moment...</title>')


def test_provider_registration_needs_no_writable_data_directory(tmp_path):
    folder=tmp_path/'missing'
    ByteTo(str(folder/'catalog.sqlite3'))
    assert not folder.exists()

@pytest.mark.parametrize('url',[
    'https://byte.to.evil.org/category/Ebooks/Test-12.html','http://byte.to/category/Ebooks/Test-12.html',
    'https://byte.to:444/category/Ebooks/Test-12.html','https://x:y@byte.to/category/Ebooks/Test-12.html',
    'https://byte.to/category/Ebooks/../../secret-12.html','https://byte.to/category/Movies/Test-12.html',
])
def test_source_origins_pinned(url):assert not source_url(url)

@pytest.mark.parametrize('url',['https://127.0.0.1/a','https://filecrypt.cc.evil.org/Container/AB.html',
    'http://filecrypt.cc/Container/AB.html','https://filecrypt.cc/Container/AB.html?next=http://127.0.0.1',
    'https://ddownload.com/free86184.html'])
def test_only_explicit_container_routes(url):assert not container_url(url)

@pytest.mark.asyncio
async def test_source_pagination_does_not_skip_175_books_and_cache_persists_records(tmp_path):
    provider=ByteTo(str(tmp_path/'db'));provider.min_interval=0;seen=[]
    def handler(r):seen.append(str(r.url));return httpx.Response(200,text=catalog(200))
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as c:
        first,more=await provider.catalog(c,page=1)
        second,_=await provider.catalog(c,page=2)
        assert first[0]['source_id']==12345 and second[0]['source_id']==12370 and more
        assert len(seen)==1
    assert ByteTo(str(tmp_path/'db')).lookup(12345)==ITEM

@pytest.mark.asyncio
async def test_widgets_resolve_only_source_loadbuttons_without_ad_or_captcha_bypass(tmp_path):
    provider=ByteTo(str(tmp_path/'db'));provider.min_interval=0;provider.remember([ITEM]);seen=[]
    def handler(r):
        seen.append(r.url.path)
        if r.url.path.startswith('/category/'):
            return httpx.Response(200,text='<iframe src="https://evil.org/a"></iframe><iframe src="/widgets/button.php?opaque"></iframe>')
        if r.url.path=='/widgets/button.php':
            return httpx.Response(200,text='<a class="loadbutton" href="/go.php?hash=opaque">Download</a><a href="https://ddownload.com/free86184.html">advert</a>')
        return httpx.Response(302,headers={'location':'https://filecrypt.cc/Container/ABC123.html'})
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as c:
        _,links=await provider.resolve(c,12345)
    assert links==['https://filecrypt.cc/Container/ABC123.html']
    assert seen==['/category/Ebooks/Faust-Goethe-12345.html','/widgets/button.php','/go.php']

def test_atomic_dispatch_duplicate_click_and_restart_do_not_add_twice(tmp_path,monkeypatch):
    store=engine(tmp_path,monkeypatch);job=store.add(ITEM)
    assert store.add(ITEM)['id']==job['id']
    job=store.claim();store.dispatch(job,['https://filecrypt.cc/Container/AB.html'])
    crawl=store.root/'watch'/(job['id']+'.crawljob');payload=json.loads(crawl.read_text())
    assert payload[0]['downloadFolder']=='/downloads/'+job['id']
    assert payload[0]['chunks']==1 and payload[0]['forcedStart']=='FALSE'
    added=store.root/'watch'/'added';added.mkdir();crawl.rename(added/crawl.name)
    store=HosterJobs(store.path)
    store.dispatch(job,['https://filecrypt.cc/Container/AB.html'])
    assert not crawl.exists() and store.list()[0]['status']=='dispatched'


def test_link_resolution_retries_survive_restart_and_stop_after_three(tmp_path,monkeypatch):
    store=engine(tmp_path,monkeypatch);job=store.add(ITEM)
    monkeypatch.setattr('royalbooks.hoster_jobs.time.time',lambda:1000)
    job=store.claim();store.resolution_failed(job,'http_503')
    assert store.list()[0]['retry_at']==1060
    assert store.claim() is None
    store=HosterJobs(store.path)
    monkeypatch.setattr('royalbooks.hoster_jobs.time.time',lambda:1060)
    job=store.claim();store.resolution_failed(job,'ReadTimeout')
    assert store.list()[0]['retry_at']==1360
    monkeypatch.setattr('royalbooks.hoster_jobs.time.time',lambda:1360)
    job=store.claim();store.resolution_failed(job,'http_503')
    assert store.list()[0]['status']=='failed' and store.list()[0]['attempts']==3
    assert store.claim() is None

def test_finished_epub_stages_but_partial_or_multiple_files_do_not(tmp_path,monkeypatch):
    store=engine(tmp_path,monkeypatch);job=store.add(ITEM)
    folder=store.root/'received'/job['id'];folder.mkdir()
    f=folder/'Faust.epub';f.write_bytes(epub());os.utime(f,(time.time()-60,)*2)
    partial=folder/'something.part';partial.touch();store.stage_received(job)
    assert store.list()[0]['status']=='queued'
    partial.unlink();store.stage_received(job)
    assert store.list()[0]['status']=='staged'
    assert (tmp_path/'staging'/(job['id']+'.epub')).is_file()

def test_no_engine_no_queue_and_ambiguous_restart_requires_review(tmp_path,monkeypatch):
    store=HosterJobs(str(tmp_path/'db'))
    with pytest.raises(ProviderError,match='not_configured'):store.add(ITEM)
    store=engine(tmp_path,monkeypatch);job=store.add(ITEM);store.claim()
    assert HosterJobs(store.path).list()[0]['status']=='needs_review'


@pytest.mark.parametrize('mode',['wrong_title','multiple','invalid'])
def test_unrelated_or_ambiguous_results_are_never_imported(tmp_path,monkeypatch,mode):
    store=engine(tmp_path,monkeypatch);job=store.add(ITEM)
    folder=store.root/'received'/job['id'];folder.mkdir()
    first=folder/'book.epub'
    first.write_bytes(b'not an epub' if mode=='invalid' else epub('Other Book' if mode=='wrong_title' else 'Faust'))
    os.utime(first,(time.time()-60,)*2)
    if mode=='multiple':
        other=folder/'other.epub';other.write_bytes(epub('Other Book'));os.utime(other,(time.time()-60,)*2)
    with pytest.raises(AcquisitionError):store.stage_received(job)
    assert not list((tmp_path/'staging').glob('*.epub'))


@pytest.mark.asyncio
async def test_hoster_api_only_accepts_indexed_ids_and_requires_admin_consent(tmp_path,monkeypatch):
    from royalbooks.app import app
    monkeypatch.setenv('ROYALBOOKS_API_TOKEN','fixture-secret')
    provider=ByteTo(str(tmp_path/'db.sqlite3'));provider.remember([ITEM])
    monkeypatch.setattr(app.state,'providers',{'byte':provider},raising=False)
    monkeypatch.setattr(app.state,'hoster_jobs',engine(tmp_path,monkeypatch),raising=False)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),base_url='http://test') as c:
        payload={'source_id':12345,'rights_confirmed':True}
        assert (await c.post('/api/hosters/jobs',json=payload)).status_code==401
        headers={'X-API-Key':'fixture-secret'}
        assert (await c.post('/api/hosters/jobs',headers=headers,json={**payload,'rights_confirmed':False})).status_code==400
        assert (await c.post('/api/hosters/jobs',headers=headers,json={**payload,'source_id':999999})).status_code==409
        assert (await c.post('/api/hosters/jobs',headers=headers,json=payload)).status_code==202
        r=await c.get('/api/hosters',headers=headers)
        assert r.status_code==200 and r.json()['jobs'][0]['book']['title']=='Faust'
        assert 'fixture-secret' not in r.text
        job_id=r.json()['jobs'][0]['id']
        endpoint='/api/hosters/jobs/'+job_id+'/links'
        manual={'links':['https://ddownload.com/abcdefghijkl'],'rights_confirmed':True}
        assert (await c.post(endpoint,json=manual)).status_code==401
        assert (await c.post(endpoint,headers=headers,json=manual)).status_code==409
        app.state.hoster_jobs.update(job_id,'needs_review',error='engine_cloudflare_blocked')
        assert (await c.post(endpoint,headers=headers,json={**manual,'rights_confirmed':False})).status_code==400
        assert (await c.post(endpoint,headers=headers,json=manual)).status_code==202
        assert (await c.post(endpoint,headers=headers,json=manual)).status_code==409

@pytest.mark.asyncio
async def test_worker_validates_and_hands_off_once(tmp_path,monkeypatch):
    store=engine(tmp_path,monkeypatch);job=store.add(ITEM)
    class Source:
        async def resolve(self,*args):return ITEM,['https://filecrypt.cc/Container/AB.html']
    folder=store.root/'received'/job['id'];folder.mkdir()
    f=folder/'Faust.epub';f.write_bytes(epub());os.utime(f,(time.time()-60,)*2)
    ingest=tmp_path/'ingest';ingest.mkdir()
    task=asyncio.create_task(worker(store,Source(),None,ingest))
    try:
        for _ in range(50):
            if store.list()[0]['status']=='delivered':break
            await asyncio.sleep(.1)
        assert store.list()[0]['status']=='delivered'
        assert len(list(ingest.glob('*.epub')))==1
    finally:
        task.cancel()
        with pytest.raises(asyncio.CancelledError):await task


def test_engine_finished_error_is_not_success_and_snapshot_contains_no_urls():
    packages=[{'uuid':1,'name':'RoyalBooks-'+'a'*32,'finished':True},
              {'uuid':2,'name':'Other user package'}]
    links=[{'packageUUID':1,'name':'Blocked by!Cloudflare Site-Protection','status':'File not found',
            'finished':False,'url':'https://example.org/private-key'},
           {'packageUUID':2,'name':'Private user file','bytesLoaded':900}]
    result=summarize(packages,links)
    assert result['a'*32]['status']=='blocked'
    assert 'private' not in json.dumps(result).lower()
    links.append({'packageUUID':1,'name':'Faust.epub','finished':True,'bytesLoaded':400})
    assert summarize(packages,links)['a'*32]['status']=='finished_unverified'


def test_manual_recovery_only_for_blocked_jobs_and_real_hoster_file_links(tmp_path,monkeypatch):
    store=engine(tmp_path,monkeypatch);job=store.add(ITEM)
    with pytest.raises(ProviderError,match='cannot_resume'):
        store.manual_links(job['id'],['https://ddownload.com/abcdefghijkl'])
    store.update(job['id'],'needs_review',error='engine_cloudflare_blocked')
    with pytest.raises(ProviderError,match='direct_file_links'):
        store.manual_links(job['id'],['https://filecrypt.cc/Container/ABC.html'])
    assert store.manual_links(job['id'],['https://ddownload.com/abcdefghijkl'])==job['id']
    assert (store.root/'watch'/(job['id']+'-manual1.crawljob')).is_file()
    with pytest.raises(ProviderError,match='cannot_resume'):
        store.manual_links(job['id'],['https://ddownload.com/abcdefghijkl'])
    store.update(job['id'],'delivered')
    with pytest.raises(ProviderError,match='cannot_resume'):
        store.manual_links(job['id'],['https://ddownload.com/abcdefghijkl'])


def test_stale_engine_status_is_not_shown_as_live(tmp_path):
    folder=tmp_path/'engine-state';folder.mkdir()
    (folder/'status.json').write_text(json.dumps({'checked_at':int(time.time())-90,'reachable':True,'jobs':{}}))
    assert read_status(tmp_path)=={}


@pytest.mark.asyncio
async def test_worker_exposes_cloudflare_stop_without_claiming_success(tmp_path,monkeypatch):
    store=engine(tmp_path,monkeypatch);job=store.add(ITEM)
    store.update(job['id'],'dispatched')
    folder=store.root/'engine-state';folder.mkdir()
    (folder/'status.json').write_text(json.dumps({'checked_at':int(time.time()),'reachable':True,
        'jobs':{job['id']:{'status':'blocked'}}}))
    task=asyncio.create_task(worker(store,object(),None,None))
    try:
        for _ in range(30):
            if store.list()[0]['status']=='needs_review':break
            await asyncio.sleep(.1)
        assert store.list()[0]['error']=='engine_cloudflare_blocked'
        assert store.list()[0]['sha256'] is None
    finally:
        task.cancel()
        with pytest.raises(asyncio.CancelledError):await task
