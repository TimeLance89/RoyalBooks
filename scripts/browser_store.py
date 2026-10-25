"""Offline store browsing, real persisted shelves, details and search handoff."""
import os,socket,subprocess,sys,tempfile,time,urllib.request
from pathlib import Path
from playwright.sync_api import sync_playwright
from royalbooks.storefront import Storefront
from royalbooks.providers import Book

ROOT=Path(__file__).resolve().parents[1]

def main():
    (ROOT/'data/screenshots').mkdir(parents=True,exist_ok=True)
    with tempfile.TemporaryDirectory() as folder:
        database=str(Path(folder)/'db.sqlite3');store=Storefront(database,None)
        books=[Book(source='libgen',source_id=f'{i:032x}',title=title,authors=[author],language='de',year=2024,cover_url=None,info_url='https://libgen.li/',formats=('EPUB',),acquisition_id=100+i).as_dict() for i,title,author in [(1,'Es','Stephen King'),(2,'Die Therapie','Sebastian Fitzek'),(3,'Verity','Colleen Hoover'),(4,'Der Wüstenplanet','Frank Herbert')]]
        for name in ['releases','recent','fiction','nonfiction']:store.save(name,books)
        trending={**books[0],'source':'openlibrary','source_id':'OL123W','formats':[],'acquisition_id':None,'download_supported':False,'epub_url':None,'title':'Tagestrend','language':None}
        store.save('trending',[trending]);store.save('preview:OL123W',{'description':'Eine echte Beschreibung aus dem Testkatalog.','source':'Open Library','scope':'work'})
        with socket.socket() as sock:sock.bind(('127.0.0.1',0));port=sock.getsockname()[1]
        base=f'http://127.0.0.1:{port}';env={**os.environ,'ROYALBOOKS_DB_PATH':database,'ROYALBOOKS_STORE_REFRESH_ENABLED':'0','ROYALBOOKS_DOWNLOADS_ENABLED':'0','ROYALBOOKS_API_TOKEN':'fixture-key','ROYALBOOKS_TRUSTED_NETWORKS':'127.0.0.1/32'}
        process=subprocess.Popen([sys.executable,'-m','uvicorn','royalbooks.app:app','--host','127.0.0.1','--port',str(port)],cwd=ROOT,env=env,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)
        try:
            for _ in range(100):
                try:urllib.request.urlopen(base+'/api/health',timeout=1);break
                except OSError:time.sleep(.1)
            else:raise RuntimeError('UI test server unavailable')
            with sync_playwright() as p:
                browser=p.chromium.launch()
                for name,width,height in [('desktop',1440,1000),('mobile',390,844),('small',320,740)]:
                    page=browser.new_page(viewport={'width':width,'height':height});errors=[]
                    page.on('pageerror',lambda error:errors.append(str(error)))
                    page.route('**/api/availability',lambda route:route.fulfill(json={'items':[{**item,'status':'available','checked_at':int(time.time()),'error':None,'size':1000} for item in route.request.post_data_json['items']]}))
                    page.route('**/api/books/search?*',lambda route:route.fulfill(json={'items':[],'sources':[],'display_groups':[]}))
                    page.goto(base);
                    if page.locator('html').get_attribute('data-theme')=='dark':page.locator('#theme-toggle').click()
                    page.locator('.store-section').last.wait_for()
                    assert page.locator('#store').is_visible() and page.locator('.store-section').count()==4
                    assert page.evaluate('document.documentElement.scrollWidth<=innerWidth')
                    page.evaluate('window.scrollTo(0,0)')
                    page.screenshot(path=str(ROOT/'data/screenshots'/('store-'+name+'.png')),full_page=True)
                    page.locator('#store-available-only').uncheck()
                    page.locator('[data-shelf-filter=trending]').click()
                    assert page.locator('.store-section').count()==1
                    page.locator('.store-card').click();assert page.locator('#book-dialog').is_visible()
                    page.wait_for_function("document.querySelector('.detail-description').textContent.includes('echte Beschreibung')")
                    assert page.locator('#detail-content').get_by_role('button',name='EPUB übernehmen',exact=True).count()==0
                    with page.expect_request(lambda req:'/api/books/search?' in req.url) as request:
                        page.get_by_role('button',name='EPUB-Ausgaben suchen',exact=True).click()
                    assert 'Tagestrend' in request.value.url
                    assert page.locator('#q').is_visible()
                    page.locator('[data-view-target=store]').click();page.locator('[data-shelf-filter=all]').click()
                    rail=page.locator('.store-rail').first
                    if width<700:
                        page.get_by_role('button',name='Weitere Bücher · Neu im EPUB-Katalog',exact=True).click()
                        page.wait_for_function("document.querySelector('.store-rail').scrollLeft>0")
                    page.locator('#theme-toggle').click();assert page.locator('html').get_attribute('data-theme')=='dark'
                    assert page.evaluate('document.documentElement.scrollWidth<=innerWidth')
                    page.evaluate('window.scrollTo(0,0)')
                    page.screenshot(path=str(ROOT/'data/screenshots'/('store-dark-'+name+'.png')),full_page=True)
                    assert not errors,errors
                    page.close();print(name+': persisted store, category filters, honest trend eligibility, synopsis, search handoff, carousel and dark mode OK')
                browser.close()
        finally:process.terminate();process.wait(timeout=10)

if __name__=='__main__':main()
