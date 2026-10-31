"""Real RoyalBooks UI/API against a local BookOrbit protocol fixture."""
import json
import os
import socket
import subprocess
import sys
import tempfile
import threading
import time
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from playwright.sync_api import sync_playwright
ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
from tests.test_bookorbit import tokens
from tests.test_webshare import epub


def main():
    calls=[]
    class Handler(BaseHTTPRequestHandler):
        def log_message(self,*args):pass
        def reply(self,data,status=200,binary=False):
            payload=data if binary else json.dumps(data).encode()
            self.send_response(status);self.send_header('Content-Type','application/epub+zip' if binary else 'application/json');self.send_header('Content-Length',str(len(payload)));self.end_headers();self.wfile.write(payload)
        def do_POST(self):
            body=json.loads(self.rfile.read(int(self.headers.get('Content-Length','0'))) or b'{}')
            calls.append((self.path,body))
            if self.path=='/api/v1/auth/login':return self.reply(tokens())
            if self.path=='/api/v1/books/query':
                assert self.headers.get('Authorization')=='Bearer access-one'
                return self.reply({'items':[{'id':17,'title':'Die Seelenburg','authors':['Jason Dark'],'addedAt':'2026-10-31','files':[{'id':23,'format':'epub'}]}],'total':1})
            return self.reply({},404)
        def do_GET(self):
            if self.path=='/api/v1/health':return self.reply({'status':'ok'})
            if self.path=='/api/v1/book-dock/summary':return self.reply({'pending':0,'ready':0})
            if self.path=='/api/v1/books/17':return self.reply({'files':[{'id':23,'format':'epub'}]})
            if self.path=='/api/v1/books/files/23/download':return self.reply(epub(),binary=True)
            return self.reply({},404)
    fixture=ThreadingHTTPServer(('127.0.0.1',0),Handler)
    thread=threading.Thread(target=fixture.serve_forever,daemon=True);thread.start()
    with tempfile.TemporaryDirectory() as folder:
        root=Path(folder);data=root/'data';data.mkdir();inbox=root/'dock';inbox.mkdir()
        secret=root/'bookorbit.env';secret.write_text('BOOKORBIT_USERNAME=royalbooks\nBOOKORBIT_PASSWORD=fixture-only\n');secret.chmod(0o600)
        with socket.socket() as sock:sock.bind(('127.0.0.1',0));port=sock.getsockname()[1]
        base=f'http://127.0.0.1:{port}'
        env={**os.environ,'ROYALBOOKS_DB_PATH':str(data/'db.sqlite'),'ROYALBOOKS_LIBRARY_BACKEND':'bookorbit',
             'ROYALBOOKS_BOOKORBIT_URL':f'http://127.0.0.1:{fixture.server_port}','ROYALBOOKS_BOOKORBIT_PUBLIC_URL':f'http://127.0.0.1:{fixture.server_port}',
             'ROYALBOOKS_BOOKORBIT_SECRET_FILE':str(secret),'ROYALBOOKS_BOOKORBIT_INGEST_PATH':str(inbox),
             'ROYALBOOKS_API_TOKEN':'fixture-only','ROYALBOOKS_TRUSTED_NETWORKS':'127.0.0.1/32',
             'ROYALBOOKS_STORE_REFRESH_ENABLED':'0','ROYALBOOKS_DOWNLOADS_ENABLED':'0'}
        process=subprocess.Popen([sys.executable,'-m','uvicorn','royalbooks.app:app','--host','127.0.0.1','--port',str(port)],cwd=ROOT,env=env,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)
        try:
            for _ in range(100):
                try:urllib.request.urlopen(base+'/api/health',timeout=1);break
                except OSError:time.sleep(.1)
            else:raise RuntimeError('RoyalBooks did not start')
            with sync_playwright() as p:
                browser=p.chromium.launch()
                for label,width,height in [('desktop',1440,1000),('mobile',390,844),('small',320,740)]:
                    page=browser.new_page(viewport={'width':width,'height':height});errors=[]
                    page.on('pageerror',lambda err:errors.append(str(err)))
                    page.goto(base+'/library')
                    page.wait_for_function("document.querySelector('#library-books').textContent.includes('Die Seelenburg')")
                    page.locator('.shelf-book button').first.click()
                    with page.expect_download() as download:page.get_by_role('link',name='EPUB auf dieses Gerät laden').click()
                    assert Path(download.value.path()).read_bytes()==epub()
                    page.keyboard.press('Escape')
                    page.goto(base+'/settings/library')
                    page.wait_for_function("document.querySelector('#setting-bookorbit-state').textContent==='Angemeldet'")
                    page.locator('#bookorbit-test').click()
                    page.wait_for_function("document.querySelector('#bookorbit-result').textContent.includes('atomare Veröffentlichung geprüft')")
                    assert not list(inbox.iterdir())
                    assert page.locator('#bookorbit-link').is_visible()
                    assert page.evaluate('document.documentElement.scrollWidth<=innerWidth')
                    assert 'Calibre' not in page.locator('body').inner_text()
                    (ROOT/'data/screenshots').mkdir(parents=True,exist_ok=True)
                    page.evaluate('window.scrollTo(0,0)')
                    page.screenshot(path=str(ROOT/f'data/screenshots/bookorbit-{label}.png'),full_page=True)
                    assert not errors,errors
                    page.close();print(label+': BookOrbit shelf, EPUB proxy, login status, storage probe and layout OK')
                browser.close()
            assert sum(path=='/api/v1/auth/login' for path,_ in calls)==1
        finally:
            process.terminate();process.wait(timeout=10)
            fixture.shutdown();fixture.server_close()


if __name__=='__main__':main()
