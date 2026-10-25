"""Real browser checks against a read-only Calibre fixture, not mocked shelf data."""
import os, socket, sqlite3, subprocess, sys, tempfile, time, urllib.request
from pathlib import Path
from playwright.sync_api import sync_playwright

ROOT=Path(__file__).resolve().parents[1]

def main():
    (ROOT/'data/screenshots').mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory() as folder:
        root=Path(folder);library=root/'calibre';library.mkdir()
        with sqlite3.connect(library/'metadata.db') as db:
            db.executescript('CREATE TABLE books(id INTEGER PRIMARY KEY,title TEXT,sort TEXT,author_sort TEXT,timestamp TEXT,path TEXT);CREATE TABLE data(book INTEGER,format TEXT,name TEXT);')
            for i,title,author in [(1,'Faust','Goethe, Johann Wolfgang'),(2,'Über den Wolken','Müller, Anna'),(3,'Die Verwandlung','Kafka, Franz'),(4,'Der Prozess','Kafka, Franz')]:
                db.execute('INSERT INTO books VALUES(?,?,?,?,?,?)',(i,title,title,author,f'2026-10-0{i}',str(i)))
                db.execute('INSERT INTO data VALUES(?,?,?)',(i,'EPUB','Book'))
                directory=library/str(i);directory.mkdir();(directory/'Book.epub').write_bytes(b'fixture-epub')
        db.close()
        with socket.socket() as sock:sock.bind(('127.0.0.1',0));port=sock.getsockname()[1]
        base=f'http://127.0.0.1:{port}'
        env={**os.environ,'ROYALBOOKS_DB_PATH':str(root/'royalbooks.sqlite3'),'ROYALBOOKS_CWA_LIBRARY_PATH':str(library),'ROYALBOOKS_API_TOKEN':'fixture-key','ROYALBOOKS_TRUSTED_NETWORKS':'127.0.0.1/32','ROYALBOOKS_DOWNLOADS_ENABLED':'0'}
        process=subprocess.Popen([sys.executable,'-m','uvicorn','royalbooks.app:app','--host','127.0.0.1','--port',str(port)],cwd=ROOT,env=env,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)
        try:
            for _ in range(100):
                try:urllib.request.urlopen(base+'/api/health',timeout=1);break
                except OSError:time.sleep(.1)
            else:raise RuntimeError('Test server failed')
            with sync_playwright() as p:
                browser=p.chromium.launch()
                for name,width,height in [('desktop',1440,1000),('mobile',390,844),('small-mobile',320,740)]:
                    page=browser.new_page(viewport={'width':width,'height':height});errors=[]
                    page.on('pageerror',lambda e:errors.append(str(e)))
                    page.goto(base)
                    page.wait_for_function("document.querySelectorAll('.shelf-book').length===4")
                    assert page.locator('[data-view=library]').is_visible()
                    assert not page.locator('#token').is_visible()
                    assert page.evaluate('document.documentElement.scrollWidth<=innerWidth')
                    page.locator('#view-list').click();assert page.locator('#library-books').evaluate("e=>e.classList.contains('list-layout')")
                    page.reload();page.wait_for_function("document.querySelectorAll('.shelf-book').length===4")
                    assert page.locator('#view-list').get_attribute('aria-pressed')=='true'
                    page.locator('#view-grid').click();page.locator('#theme-toggle').click()
                    assert page.locator('html').get_attribute('data-theme')=='dark'
                    page.reload();page.wait_for_function("document.querySelectorAll('.shelf-book').length===4")
                    assert page.locator('html').get_attribute('data-theme')=='dark'
                    page.locator('#theme-toggle').click()
                    page.locator('#library-query').fill('MÜLLER')
                    page.wait_for_function("document.querySelectorAll('.shelf-book').length===1")
                    assert 'Über den Wolken' in page.locator('#library-books').inner_text()
                    page.locator('.shelf-book button').click();assert page.locator('#book-dialog').is_visible()
                    with page.expect_download() as event:page.get_by_role('link',name='EPUB auf dieses Gerät laden').click()
                    assert Path(event.value.path()).read_bytes()==b'fixture-epub'
                    page.keyboard.press('Escape')
                    page.locator('#library-query').fill('kein-treffer');page.wait_for_function("document.querySelector('#library-books').textContent.includes('Keine passenden')")
                    page.get_by_role('button',name='Bücher entdecken').click();assert page.locator('#q').is_visible()
                    for view in ['watchlist','download-center','library']:
                        page.locator('[data-view-target='+view+']').click()
                        assert page.evaluate('document.documentElement.scrollWidth<=innerWidth')
                    page.locator('#library-query').fill('');page.wait_for_function("document.querySelectorAll('.shelf-book').length===4")
                    page.screenshot(path=str(ROOT/'data/screenshots'/('shelf-'+name+'.png')),full_page=True)
                    assert not errors,errors
                    page.close();print(name+': shelf, navigation, Unicode search, layout/theme persistence, EPUB transfer OK')
                browser.close()
        finally:process.terminate();process.wait(timeout=10)

if __name__=='__main__':main()
