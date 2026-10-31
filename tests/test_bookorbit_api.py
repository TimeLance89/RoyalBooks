import httpx
import pytest

from royalbooks.app import app
from royalbooks.bookorbit import Client,Config
from royalbooks.jobs import JobStore
from tests.test_bookorbit import secret,tokens


@pytest.mark.asyncio
async def test_authenticated_api_connection_library_and_storage(tmp_path,monkeypatch):
    data=tmp_path/'data';data.mkdir();inbox=tmp_path/'inbox';inbox.mkdir()
    monkeypatch.setenv('ROYALBOOKS_API_TOKEN','test-key')
    monkeypatch.setenv('ROYALBOOKS_BOOKORBIT_INGEST_PATH',str(inbox))
    monkeypatch.setenv('ROYALBOOKS_BOOKORBIT_URL','http://orbit:3000')
    monkeypatch.setenv('ROYALBOOKS_DB_PATH',str(data/'db'))
    monkeypatch.delenv('ROYALBOOKS_TRUSTED_NETWORKS',raising=False)
    monkeypatch.setattr(app.state,'jobs',JobStore(str(data/'db')),raising=False)
    def handler(r):
        if r.url.path.endswith('/auth/login'):return httpx.Response(200,json=tokens())
        if r.url.path.endswith('/health'):return httpx.Response(200,json={'status':'ok'})
        if r.url.path.endswith('/books/query'):return httpx.Response(200,json={'items':[{'id':4,'title':'A Book','authors':['Writer'],'files':[{'id':5,'format':'epub'}]}],'total':1})
        return httpx.Response(200,json={'pending':0,'ready':0})
    orbit=Client(Config(url='http://orbit:3000',secret_file=secret(tmp_path)),transport=httpx.MockTransport(handler))
    monkeypatch.setattr(app.state,'bookorbit',orbit,raising=False)
    try:
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),base_url='http://test') as c:
            assert (await c.get('/api/bookorbit/status')).status_code==401
            headers={'X-API-Key':'test-key'}
            result=(await c.get('/api/bookorbit/status',headers=headers)).json()
            assert result['authenticated'] and result['reachable']
            result=(await c.post('/api/bookorbit/test',headers=headers)).json()
            assert result['connection']['authenticated']
            assert any(row['name']=='bookorbit_write_probe' and row['status']=='ok' for row in result['storage']['checks'])
            library=(await c.get('/api/library',headers=headers)).json()
            assert library['items'][0]['title']=='A Book'
            assert not list(inbox.iterdir())
            assert 'test-only' not in str(result) and 'access-one' not in str(result)
    finally:await orbit.close()


def test_nas_audit_distinguishes_real_shared_paths():
    from scripts.nas_bookorbit_audit import audit
    orbit={'Config':{'Env':['BOOK_DOCK_PATH=','DB_PASSWORD=never-print']},'State':{'Running':True},
           'Mounts':[{'Type':'volume','Destination':'/data','Source':'/vol/data','RW':True},
                     {'Type':'bind','Destination':'/books','Source':'/books','RW':True}]}
    royal={'State':{'Running':True},'Mounts':[{'Type':'volume','Destination':'/bookorbit-ingest','Source':'/vol/data/book-dock','RW':True}]}
    report=audit(orbit,royal)
    assert all(c['status']=='ok' for c in report['checks'])
    assert 'never-print' not in str(report)
    royal['Mounts'][0]['Source']='/vol/data'
    royal['HostConfig']={'Mounts':[{'Target':'/bookorbit-ingest','VolumeOptions':{'Subpath':'book-dock'}}]}
    assert all(c['status']=='ok' for c in audit(orbit,royal)['checks'])
    royal['Mounts'][0]['Source']='/wrong'
    assert audit(orbit,royal)['status']=='error'
