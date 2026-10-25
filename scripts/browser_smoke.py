"""Real Chromium UI checks with explicit catalog fixtures and real private API storage."""
import argparse
import json
import os
import socket
import subprocess
import sys
import tempfile
import time
import urllib.request
from pathlib import Path

from playwright.sync_api import sync_playwright
from royalbooks.auth import SessionStore

ROOT = Path(__file__).resolve().parents[1]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--screenshots', type=Path, default=ROOT/'data/screenshots')
    args = parser.parse_args()
    args.screenshots.mkdir(parents=True, exist_ok=True)
    with socket.socket() as sock:
        sock.bind(('127.0.0.1', 0))
        port = sock.getsockname()[1]
    with tempfile.TemporaryDirectory() as folder:
        token = 'browser-fixture-private-token'
        env = {**os.environ, 'ROYALBOOKS_DB_PATH': str(Path(folder)/'db.sqlite3'),
               'ROYALBOOKS_API_TOKEN': token, 'ROYALBOOKS_STORE_REFRESH_ENABLED':'0', 'ROYALBOOKS_DOWNLOADS_ENABLED': '0'}
        process = subprocess.Popen([sys.executable, '-m', 'uvicorn', 'royalbooks.app:app',
                                    '--host', '127.0.0.1', '--port', str(port)],
                                   cwd=ROOT, env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        base = f'http://127.0.0.1:{port}'
        try:
            for _ in range(100):
                try:
                    urllib.request.urlopen(base+'/api/health', timeout=1)
                    break
                except OSError:
                    time.sleep(.1)
            else:
                raise RuntimeError('UI test server failed to start')
            with sync_playwright() as browser_runtime:
                browser = browser_runtime.chromium.launch()
                for name, width, height in [('desktop', 1440, 1000), ('mobile', 390, 844)]:
                    page = browser.new_page(viewport={'width': width, 'height': height})
                    page.on('dialog', lambda dialog: dialog.accept())
                    errors = []
                    page.on('pageerror', lambda error: errors.append(str(error)))
                    page.route('**/api/availability',lambda route:route.fulfill(json={'items':[{**item,'status':'available','checked_at':int(time.time()),'error':None,'size':1000} for item in route.request.post_data_json['items']]}))
                    book = dict(source='dnb', source_id='123456789', title='Faust', authors=['Goethe, Johann'],
                                language='de', year=1808, info_url='https://d-nb.info/123456789',
                                isbns=['9783551551672'], cover_url=None, file_evidence='metadata_only')
                    edition = {**book, 'source_id':'987654321', 'isbns':['9783531157887']}
                    file = {**book, 'source':'wikisource', 'source_id':'123', 'epub_url':'https://ws-export-app.wmcloud.org/',
                            'formats':['EPUB'], 'file_evidence':'listed_unverified','download_supported':True}
                    data = dict(items=[book, edition, file],
                                display_groups=[dict(primary_index=2,item_indexes=[0,1,2],epub_count=1)],
                                sources=[dict(provider='dnb',status='found'),dict(provider='gutendex',status='error',error='http_403')],
                                search_evidence=dict(epub_links_listed_unverified=1, file_listings_unverified=0))
                    page.route('**/api/books/search?*', lambda route: route.fulfill(json=data))
                    page.goto(base+'/#search')
                    assert page.locator('#language').input_value() == 'de'
                    page.locator('[data-view-target=search]').click()
                    page.locator('#q').fill('Faust')
                    page.locator('#go').click()
                    page.locator('#books .book').wait_for()
                    assert page.locator('#books .book').count() == 1
                    assert 'EPUB' in page.locator('#books').inner_text()
                    assert page.locator('#token').is_visible() is False
                    assert page.evaluate('document.documentElement.scrollWidth <= innerWidth')
                    page.evaluate('window.scrollTo(0,0)')
                    page.screenshot(path=str(args.screenshots/(name+'.png')), full_page=True)
                    page.get_by_role('button', name='Buchdetails', exact=True).click()
                    assert page.locator('#book-dialog').is_visible()
                    assert '9783551551672' in page.locator('#detail-content').inner_text()
                    assert '9783531157887' in page.locator('#detail-content').inner_text()
                    page.keyboard.press('Escape')
                    assert not page.locator('#book-dialog').is_visible()
                    page.locator('#books').get_by_role('button', name='EPUB übernehmen', exact=True).click()
                    assert page.locator('#admin-access').evaluate('element=>element.open')
                    assert page.locator('#token').evaluate('element=>element===document.activeElement')
                    page.locator('#token').fill(token)
                    page.locator('#access-login').click()
                    page.wait_for_function("document.querySelector('#access-status').textContent.includes('ist für Downloads')")
                    assert page.locator('#token').input_value() == ''
                    page.reload()
                    page.wait_for_function("document.querySelector('#access-status').textContent.includes('ist für Downloads')")
                    page.locator('[data-view-target=search]').click()
                    page.locator('#q').fill('Faust')
                    page.locator('#go').click()
                    page.locator('#books .book').wait_for()
                    page.locator('#books').get_by_role('button', name='Auf Merkliste', exact=True).click()
                    page.wait_for_function("document.querySelector('#watchlist-items').textContent.includes('Faust')")
                    page.locator('[data-view-target=watchlist]:visible').click()
                    page.locator('#watchlist-items').get_by_role('button', name='Entfernen').click()
                    page.wait_for_function("!document.querySelector('#watchlist-items').textContent.includes('Faust')")
                    page.locator('[data-view-target=search]').click()
                    page.locator('[data-filter=metadata]').click()
                    assert page.locator('#books .book').count() == 0
                    page.locator('[data-filter=all]').click()
                    assert page.locator('#books .book').count() == 1
                    # A real file listing need not already expose its transient
                    # download URL. LibGen uses a separate server-bound ID.
                    libgen = {**book, 'source':'libgen', 'source_id':'a'*32,
                              'formats':['EPUB'], 'file_evidence':'listed_unverified',
                              'acquisition_id':6002, 'download_supported':True}
                    data['items'] = [libgen]
                    data['display_groups'] = [dict(primary_index=0,item_indexes=[0],epub_count=1)]
                    data['search_evidence']['epub_acquisition_candidates'] = 1
                    page.locator('#go').click()
                    page.wait_for_function("document.querySelector('#books').textContent.includes('Library Genesis')")
                    page.locator('[data-filter=epub]').click()
                    assert page.locator('#books .book').count() == 1
                    page.route('**/api/jobs', lambda route: route.fulfill(status=202, json={'job':{'status':'queued'}})
                               if route.request.method == 'POST' else route.continue_())
                    with page.expect_request(lambda request: '/api/jobs' in request.url and request.method=='POST') as posted:
                        page.locator('#books').get_by_role('button', name='EPUB übernehmen', exact=True).click()
                    assert posted.value.post_data_json == {'provider':'libgen','source_id':6002,'rights_confirmed':True}
                    page.locator('[data-view-target=search]').click()
                    # The added community source uses the same queue flow.
                    data['items']=[{**libgen,'source':'memoryoftheworld','source_id':'6003','acquisition_id':6003,
                                    'epub_url':'https://library.memoryoftheworld.org/files/test/Author/Book/book.epub'}]
                    data['query']='Stephen King es'
                    data['query_analysis']={'original_query':'Steven King es'}
                    page.locator('#q').fill('Steven King es')
                    page.locator('#result-limit').select_option('60')
                    with page.expect_request(lambda request:'/api/books/search?' in request.url) as searched:
                        page.locator('#go').click()
                    assert 'limit=60' in searched.value.url
                    page.wait_for_function("document.querySelector('#books').textContent.includes('Memory of the World')")
                    assert page.locator('#search-notice').is_visible()
                    assert page.evaluate('document.documentElement.scrollWidth <= innerWidth')
                    with page.expect_request(lambda request:'/api/jobs' in request.url and request.method=='POST') as posted:
                        page.locator('#books').get_by_role('button',name='EPUB übernehmen',exact=True).click()
                    assert posted.value.post_data_json == {'provider':'memoryoftheworld','source_id':6003,'rights_confirmed':True}
                    page.locator('[data-view-target=search]').click()
                    page.unroute('**/api/books/search?*')
                    page.route('**/api/books/search?*', lambda route: route.fulfill(status=503, json={'detail':'search_busy'}))
                    page.locator('#go').click()
                    page.wait_for_function("document.querySelector('#message').textContent.includes('fehlgeschlagen')")
                    assert page.locator('#go').is_enabled()
                    # A different device receives its own one-use link; it does
                    # not depend on the browser where the admin entered a token.
                    code = SessionStore(str(Path(folder)/'db.sqlite3')).create_device_link(token)
                    fresh = browser.new_page(viewport={'width':width,'height':height})
                    fresh.on('pageerror',lambda error: errors.append(str(error)))
                    fresh.goto(base+'/connect#code='+code)
                    fresh.wait_for_function("!document.querySelector('#access-banner').hidden")
                    assert fresh.url == base+'/'
                    assert code not in fresh.content() and token not in fresh.content()
                    fresh.reload()
                    fresh.wait_for_function("!document.querySelector('#access-banner').hidden")
                    assert fresh.locator('#token').input_value() == ''
                    # Reopening the consumed link on the same device preserves login.
                    fresh.goto(base+'/connect#code='+code)
                    fresh.wait_for_function("!document.querySelector('#access-banner').hidden")
                    fresh.close()
                    assert not errors, errors
                    print(name+': search, grouping, ISBNs, details, watchlist, filters, error recovery, layout OK')
                    page.close()
                browser.close()
        finally:
            process.terminate()
            process.wait(timeout=10)


if __name__ == '__main__':
    main()
