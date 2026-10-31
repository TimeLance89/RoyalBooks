"""Royal-style navigation, persistent settings, author pages and file-only offers."""
import os,socket,subprocess,sys,tempfile,time,urllib.request
from pathlib import Path
from urllib.parse import urlsplit,parse_qs
from playwright.sync_api import sync_playwright
from royalbooks.settings import PlatformSettings,DEFAULTS

ROOT=Path(__file__).resolve().parents[1]

def main():
    (ROOT/'data/screenshots').mkdir(parents=True,exist_ok=True)
    with tempfile.TemporaryDirectory() as folder:
        database=str(Path(folder)/'db.sqlite3');preferences=PlatformSettings(database)
        with socket.socket() as sock:sock.bind(('127.0.0.1',0));port=sock.getsockname()[1]
        base=f'http://127.0.0.1:{port}'
        env={**os.environ,'ROYALBOOKS_DB_PATH':database,'ROYALBOOKS_STORE_REFRESH_ENABLED':'0','ROYALBOOKS_DOWNLOADS_ENABLED':'0','ROYALBOOKS_API_TOKEN':'fixture-key','ROYALBOOKS_TRUSTED_NETWORKS':'127.0.0.1/32'}
        process=subprocess.Popen([sys.executable,'-m','uvicorn','royalbooks.app:app','--host','127.0.0.1','--port',str(port)],cwd=ROOT,env=env,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)
        try:
            for _ in range(100):
                try:urllib.request.urlopen(base+'/api/health',timeout=1);break
                except OSError:time.sleep(.1)
            else:raise RuntimeError('Server failed')
            def make_book(title,id,source='libgen',available=True):
                return {'source':source,'source_id':f'{id:032x}' if source=='libgen' else str(id),'title':title,'authors':['Stephen King'],'language':'de','year':2024,'cover_url':None,'info_url':'https://libgen.li/','formats':['EPUB'] if source=='libgen' else [],'acquisition_id':id if source=='libgen' else None,'download_supported':source=='libgen','description':'Eine Beschreibung aus dem kontrollierten Testkatalog.','isbns':['9783551551672'],'file_evidence':'listed_unverified' if source=='libgen' else 'metadata_only'}
            items=[make_book('Es',101),make_book('Shining',102),make_book('The Stand',201,'dnb')]
            with sync_playwright() as p:
                browser=p.chromium.launch()
                for name,width,height in [('desktop',1440,1000),('mobile',390,844),('small',320,740)]:
                    preferences.save(DEFAULTS)
                    page=browser.new_page(viewport={'width':width,'height':height});errors=[];queries=[]
                    page.on('pageerror',lambda error:errors.append(str(error)))
                    def search_route(route):
                        params=parse_qs(urlsplit(route.request.url).query);queries.append(params)
                        if params.get('mode')==['isbn'] and params.get('q')==['1234567890123']:return route.continue_()
                        rows=[make_book('Carrie',103)] if params.get('page')==['2'] else items
                        route.fulfill(json={'query':params['q'][0],'items':rows,'sources':[],'has_more':params.get('page')!=['2'],'display_groups':[{'primary_index':i,'item_indexes':[i],'epub_count':int(b['download_supported'])} for i,b in enumerate(rows)]})
                    page.route('**/api/books/search?*',search_route)
                    page.route('**/api/availability',lambda route:route.fulfill(json={'items':[{**item,'status':'unavailable' if item['record_id']==102 else 'available','checked_at':int(time.time()),'error':None,'size':1000} for item in route.request.post_data_json['items']]}))
                    page.goto(base+'/authors')
                    page.locator('#author-q').fill('Stephen King');page.locator('#author-go').click()
                    page.wait_for_function("document.querySelectorAll('#author-books .book').length===1")
                    assert queries[-1]['mode']==['author']
                    assert 'Es' in page.locator('#author-books').inner_text()
                    page.locator('#author-available-only').uncheck();assert page.locator('#author-books .book').count()==3
                    shining=page.locator('#author-books .book').filter(has=page.get_by_role('heading',name='Shining',exact=True))
                    assert shining.get_by_role('button',name='EPUB übernehmen',exact=True).is_disabled()
                    page.locator('#author-more').click();page.wait_for_function("document.querySelectorAll('#author-books .book').length===4")
                    page.evaluate('window.scrollTo(0,0)')
                    page.screenshot(path=str(ROOT/'data/screenshots'/('platform-author-'+name+'.png')),full_page=True)
                    page.locator('.shell-settings').click();assert '/settings/' in page.url
                    page.locator('[data-settings-tab=sources]').click();page.wait_for_selector('[data-source-choice=libgen]')
                    assert page.url.endswith('/settings/sources')
                    for check in page.locator('[data-source-choice]').all():
                        if check.get_attribute('data-source-choice') in ['libgen','openlibrary']:check.check()
                        else:check.uncheck()
                    page.locator('[data-settings-tab=general]').click();page.locator('#pref-language').select_option('en')
                    page.locator('#pref-available-only').uncheck();page.locator('#preferences-save').click()
                    page.wait_for_function("document.querySelector('#preferences-message').textContent.includes('Gespeichert')")
                    page.reload();page.wait_for_function("document.querySelector('#pref-language').value==='en'")
                    assert not page.locator('#pref-available-only').is_checked()
                    assert PlatformSettings(database).get()['sources']==['libgen','openlibrary']
                    assert not page.locator('#q').is_visible()
                    assert page.evaluate('document.documentElement.scrollWidth<=innerWidth')
                    page.evaluate('window.scrollTo(0,0)')
                    page.screenshot(path=str(ROOT/'data/screenshots'/('platform-settings-'+name+'.png')),full_page=True)
                    page.locator('[data-settings-tab=sources]').click();assert not page.locator('[data-source-choice=dnb]').is_checked()
                    page.locator('[data-view-target=search]').click();page.locator('[data-search-mode=isbn]').click()
                    page.locator('#q').fill('1234567890123');page.locator('#go').click()
                    page.wait_for_function("document.querySelector('#message').textContent.includes('gültige ISBN')")
                    page.locator('#q').fill('9783551551672');page.locator('#go').click();page.wait_for_selector('#books .book')
                    assert queries[-1]['mode']==['isbn']
                    page.locator('.shell-settings').click();page.go_back();assert page.locator('#q').is_visible()
                    assert page.evaluate('document.documentElement.scrollWidth<=innerWidth')
                    page.route('**/api/genres',lambda route:route.fulfill(json={'genres':[{'id':'fantasy','title':'Fantasy','color':'#8778af'},{'id':'crime','title':'Krimi','color':'#a36e7a'}]}))
                    genre_items=[make_book('Nebelwald',101),make_book('Nebeltal',102),make_book('Katalogwerk',201,'openlibrary')]
                    def genre_route(route):
                        params=parse_qs(urlsplit(route.request.url).query)
                        rows=[make_book('Weitere Geschichte',103)] if params.get('page')==['2'] else genre_items
                        route.fulfill(json={'genre':'fantasy','title':'Fantasy','items':rows,'has_more':params.get('page')!=['2'],'sources':[]})
                    page.route('**/api/genres/fantasy?*',genre_route)
                    image_state={'ready':False,'routes':[]}
                    svg='<svg xmlns="http://www.w3.org/2000/svg" width="40" height="60"><rect width="40" height="60" fill="blue"/></svg>'
                    def cover_route(route):
                        if image_state['ready']:route.fulfill(content_type='image/svg+xml',body=svg)
                        else:image_state['routes'].append(route)
                    page.route('**/999-L.jpg',cover_route)
                    genre_items[0]['cover_url']='https://covers.openlibrary.org/b/id/999-L.jpg'
                    page.goto(base+'/genres');page.locator('[data-genre=fantasy]').click()
                    page.wait_for_function("document.querySelectorAll('#genre-books .book').length===3")
                    assert page.url.endswith('/genres?genre=fantasy&language=de')
                    first_cover=page.locator('#genre-books .cover-slot').first
                    assert first_cover.locator('.cover-fallback').is_visible()
                    page.wait_for_function("document.querySelector('#genre-books .cover').src.includes('999-L.jpg')")
                    image_state['ready']=True
                    for image_route in image_state['routes']:image_route.fulfill(content_type='image/svg+xml',body=svg)
                    page.wait_for_selector('#genre-books .cover-ready')
                    assert first_cover.locator('.cover-fallback').is_hidden()
                    page.locator('#genre-available-only').check()
                    page.wait_for_function("document.querySelectorAll('#genre-books .book').length===1")
                    page.locator('#genre-more').click()
                    page.wait_for_function("document.querySelectorAll('#genre-books .book').length===2")
                    page.reload();page.wait_for_function("document.querySelectorAll('#genre-books .book').length===3")
                    assert page.locator('#genre-title').inner_text()=='Fantasy'
                    assert page.evaluate('document.documentElement.scrollWidth<=innerWidth')
                    page.screenshot(path=str(ROOT/'data/screenshots'/('genres-'+name+'.png')))
                    if name=='desktop':
                        many=[make_book('Werk '+str(i),1000+i) for i in range(101)]
                        page.route('**/api/books/search?*',lambda route:route.fulfill(json={'query':'Stephen King','items':many,'sources':[],'has_more':False}))
                        page.goto(base+'/authors');page.locator('#author-q').fill('Stephen King');page.locator('#author-go').click()
                        page.wait_for_function("!document.querySelector('#author-go').disabled")
                        page.locator('#author-available-only').check()
                        page.wait_for_function("document.querySelectorAll('#author-books .book').length===101",timeout=15000)
                    page.route('**/api/jobs',lambda route:route.fulfill(json={
                        'downloads_enabled':True,'legacy_worker_enabled':False,
                        'jobs':[{'id':'a'*32,'source':'libgen','source_id':1,
                            'status':'queued','v2_authorized':1,'attempts':1,
                            'error':'http_503','retry_at':int(time.time())+60,
                            'book':{'title':'Automatischer Download','authors':['Testautor']}}]}))
                    page.goto(base+'/downloads')
                    page.wait_for_selector('#job-rows tr')
                    assert 'Wiederholung geplant' in page.locator('#job-rows').inner_text()
                    assert 'Automatischer nächster Versuch um' in page.locator('#job-rows').inner_text()
                    assert page.get_by_role('button',name='Erneut versuchen',exact=True).count()==0
                    assert page.evaluate('document.documentElement.scrollWidth<=innerWidth')
                    hoster_posts=[]
                    page.route('**/api/hosters/catalog?*',lambda route:route.fulfill(json={
                        'items':[{'source_id':12345,'title':'Faust','authors':['Goethe'],
                            'info_url':'https://byte.to/category/Ebooks/Faust-Goethe-12345.html'}],
                        'has_more':False}))
                    page.route('**/api/hosters',lambda route:route.fulfill(json={
                        'configured':True,'engine':{'reachable':True},'control_url':'https://192.168.178.47:8095','jobs':[{'id':'b'*32,'status':'needs_review','error':'engine_cloudflare_blocked','book':{'title':'Faust','info_url':'https://byte.to/category/Ebooks/Faust-Goethe-12345.html'}}]}))
                    def hoster_job(route):
                        hoster_posts.append(route.request.post_data_json)
                        route.fulfill(status=202,json={'job':{'status':'queued'}})
                    page.route('**/api/hosters/jobs',hoster_job)
                    page.goto(base+'/hosters');page.wait_for_selector('#hoster-books .book')
                    assert page.locator('#hosters').is_visible()
                    assert 'noch nicht geprüft' in page.locator('#hoster-catalog-message').inner_text()
                    page.once('dialog',lambda d:d.accept())
                    page.get_by_role('button',name='Über Hoster laden',exact=True).click()
                    page.wait_for_function("document.querySelector('#hoster-engine-message').textContent.includes('eingerichtet')")
                    assert hoster_posts==[{'source_id':12345,'rights_confirmed':True}]
                    assert page.locator('#hoster-control').is_visible()
                    assert 'Cloudflare' in page.locator('#hoster-jobs').inner_text()
                    recovered=[]
                    def recover_link(route):
                        recovered.append(route.request.post_data_json);route.fulfill(status=202,json={'accepted':True})
                    page.route('**/api/hosters/jobs/*/links',recover_link)
                    page.get_by_role('textbox',name='Hoster-Dateilinks für Faust').fill('https://ddownload.com/abcdefghijkl')
                    with page.expect_request(lambda req:req.url.endswith('/links') and req.method=='POST'):
                        page.get_by_role('button',name='Mit Hosterlink fortsetzen').click()
                    assert recovered==[{'links':['https://ddownload.com/abcdefghijkl'],'rights_confirmed':True}]
                    assert page.evaluate('document.documentElement.scrollWidth<=innerWidth')
                    fresh=browser.new_page(viewport={'width':width,'height':height})
                    fresh.route('**/api/hosters/catalog?*',lambda route:route.fulfill(json={'items':[],'has_more':False}))
                    fresh.route('**/api/hosters',lambda route:route.fulfill(json={'configured':True,'control_url':'https://192.168.178.47:8095','jobs':[]}))
                    fresh.goto(base+'/hosters')
                    fresh.wait_for_function("document.querySelector('#hoster-engine-message').textContent.includes('eingerichtet')")
                    fresh.close()
                    assert not errors,errors
                    page.close();print(name+': author pages/paging, file-only offers, unavailable-download gating, settings persistence, separate routes, ISBN validation and browser history OK')
                browser.close()
        finally:process.terminate();process.wait(timeout=10)

if __name__=='__main__':main()
