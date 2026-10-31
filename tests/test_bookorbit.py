"""BookOrbit API contract, session rotation, handoff integrity and migration."""
import asyncio
import hashlib
import json
import os
import sqlite3
import time
from datetime import datetime, timedelta, timezone

import httpx
import pytest

from royalbooks.bookorbit import Client, Config, Handoffs, BookOrbitError, credentials
from royalbooks.jobs import JobStore, deliver_staged, validate_epub
from royalbooks.preflight import diagnose
from tests.test_webshare import epub


def secret(tmp_path):
    path = tmp_path / 'bookorbit.env'
    path.write_text('BOOKORBIT_USERNAME=royalbooks\nBOOKORBIT_PASSWORD="test-only-$literal#password"\n')
    path.chmod(0o600)
    return str(path)


def tokens(access='access-one'):
    return {'accessToken': access, 'refreshToken': 'a' * 64,
            'accessTokenExpiresAt': (datetime.now(timezone.utc) + timedelta(minutes=15)).isoformat()}


def staged(tmp_path, content=None):
    data = tmp_path / 'data';data.mkdir(exist_ok=True)
    inbox = tmp_path / 'inbox';inbox.mkdir(exist_ok=True)
    stage = data / 'staging';stage.mkdir(exist_ok=True)
    store = JobStore(str(data / 'db.sqlite'))
    job = store.add(42, v2_authorized=True)
    path = stage / (job['id'] + '.epub');path.write_bytes(content or epub())
    store.update(job['id'], 'staged', sha256=hashlib.sha256(path.read_bytes()).hexdigest(), file_size=path.stat().st_size)
    return store, store.get(job['id']), stage, inbox


@pytest.mark.asyncio
async def test_login_is_singleflight_and_refresh_replays_only_once(tmp_path):
    calls = []
    rejected = False
    async def handler(request):
        nonlocal rejected
        calls.append(request)
        if request.url.path.endswith('/auth/login'):
            body = json.loads(request.content)
            assert body == {'username':'royalbooks','password':'test-only-$literal#password',
                            'clientKind':'native','deviceLabel':'RoyalBooks'}
            await asyncio.sleep(.01)
            return httpx.Response(200, json=tokens())
        if request.url.path.endswith('/auth/refresh'):
            assert json.loads(request.content) == {'refreshToken':'a'*64}
            return httpx.Response(200, json=tokens('access-two'))
        assert 'cookie' not in request.headers
        if rejected and request.headers['authorization'] == 'Bearer access-one':
            return httpx.Response(401)
        return httpx.Response(200, json={'pending':0,'ready':0})
    client = Client(Config(url='http://bookorbit:3000',secret_file=secret(tmp_path)),transport=httpx.MockTransport(handler))
    try:
        await asyncio.gather(*(client.request('GET','book-dock/summary') for _ in range(6)))
        assert sum(r.url.path.endswith('/auth/login') for r in calls) == 1
        rejected = True
        await asyncio.gather(*(client.request('GET','book-dock/summary') for _ in range(4)))
        assert sum(r.url.path.endswith('/auth/refresh') for r in calls) == 1
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_expired_refresh_reauthenticates_and_bad_login_has_cooldown(tmp_path):
    calls = []
    def handler(r):
        calls.append(r.url.path)
        if r.url.path.endswith('/auth/refresh'):return httpx.Response(401)
        if r.url.path.endswith('/auth/login'):return httpx.Response(200,json=tokens())
        return httpx.Response(200,json={})
    client = Client(Config(url='http://bookorbit:3000',secret_file=secret(tmp_path)),transport=httpx.MockTransport(handler))
    try:
        client._refresh='b'*64
        await client.request('GET','book-dock/summary')
        assert calls[:2] == ['/api/v1/auth/refresh','/api/v1/auth/login']
    finally:await client.close()
    client = Client(Config(url='http://bookorbit:3000',secret_file=secret(tmp_path)),transport=httpx.MockTransport(lambda r:httpx.Response(401)))
    try:
        with pytest.raises(BookOrbitError,match='bookorbit_http_401'):await client.request('GET','books/1')
        with pytest.raises(BookOrbitError,match='bookorbit_auth_cooldown'):await client.request('GET','books/1')
    finally:await client.close()


@pytest.mark.asyncio
async def test_no_login_on_missing_secret_health_still_reports_reachable(tmp_path):
    def handler(r):
        assert r.url.path.endswith('/health')
        return httpx.Response(200,json={'status':'ok'})
    client=Client(Config(url='http://bookorbit:3000',secret_file=str(tmp_path/'missing')),transport=httpx.MockTransport(handler))
    try:
        result=await client.connection()
        assert result['reachable'] and not result['authenticated']
        assert result['error']=='bookorbit_credentials_missing'
        assert 'password' not in json.dumps(result)
    finally:await client.close()


def test_secrets_are_data_not_shell_code_and_permissions_checked(tmp_path):
    path=secret(tmp_path)
    assert credentials(path)['BOOKORBIT_PASSWORD']=='test-only-$literal#password'
    if os.name!='nt':
        os.chmod(path,0o644)
        with pytest.raises(BookOrbitError,match='permissions'):credentials(path)
    with pytest.raises(BookOrbitError,match='missing'):credentials(str(tmp_path/'absent'))


@pytest.mark.asyncio
async def test_real_api_paths_library_paging_and_file_proxy(tmp_path):
    payload=epub();calls=[]
    def handler(r):
        calls.append(str(r.url))
        if r.url.path.endswith('/auth/login'):return httpx.Response(200,json=tokens())
        if r.url.path.endswith('/books/query'):
            q=json.loads(r.content);page=q['pagination']['page'];size=q['pagination']['size']
            assert q['sort']==[{'field':'author','dir':'asc'}]
            return httpx.Response(200,json={'items':[{'id':i+1,'title':f'Book {i+1}','authors':['Writer'],'files':[{'id':i+100,'format':'epub'}]} for i in range(page*size,(page+1)*size)],'total':10})
        if r.url.path.endswith('/books/3'):return httpx.Response(200,json={'files':[{'id':102,'format':'epub'}]})
        assert r.url.path=='/api/v1/books/files/102/download'
        return httpx.Response(200,content=payload)
    client=Client(Config(url='http://bookorbit:3000',secret_file=secret(tmp_path)),transport=httpx.MockTransport(handler))
    try:
        shelf=await client.shelf('Writer','author',limit=3,offset=2)
        assert [b['id'] for b in shelf['items']]==[3,4,5] and shelf['has_more']
        assert await client.asset(3,'epub')==payload
    finally:await client.close()


def test_successful_handoff_is_idempotent_and_missing_file_is_unconfirmed(tmp_path):
    store,job,stage,inbox=staged(tmp_path)
    ledger=Handoffs(store.path,inbox)
    deliver_staged(job,store,stage,inbox)
    assert store.get(job['id'])['status']=='delivered'
    dest=next(inbox.glob('*.epub'))
    assert validate_epub(dest)==job['sha256']
    assert not list(inbox.glob('*.part'))
    assert ledger.describe(store.get(job['id']))['status']=='dock_present'
    dest.unlink()
    # A restart and repeated delivery call cannot cause another import.
    recovered=JobStore(store.path)
    deliver_staged(recovered.get(job['id']),recovered,stage,inbox)
    assert not list(inbox.iterdir())
    assert ledger.describe(recovered.get(job['id']))['status']=='import_unconfirmed'


@pytest.mark.parametrize('mode',['corrupt','partial','metadata','collision','permissions','write_permissions','copy_changed'])
def test_bad_files_and_failed_handoff_never_publish(tmp_path,monkeypatch,mode):
    store,job,stage,inbox=staged(tmp_path)
    source=stage/(job['id']+'.epub')
    if mode=='corrupt':source.write_bytes(b'broken archive')
    if mode=='partial':source.write_bytes(source.read_bytes()[:70])
    if mode=='metadata':
        import io,zipfile
        out=io.BytesIO()
        with zipfile.ZipFile(io.BytesIO(source.read_bytes())) as original,zipfile.ZipFile(out,'w') as fixed:
            for entry in original.infolist():fixed.writestr(entry,b'<broken' if entry.filename=='content.opf' else original.read(entry))
        source.write_bytes(out.getvalue());store.update(job['id'],'staged',sha256=hashlib.sha256(out.getvalue()).hexdigest(),file_size=len(out.getvalue()));job=store.get(job['id'])
    if mode=='collision':(inbox/('royalbooks-'+job['id']+'.epub')).write_bytes(b'do not overwrite')
    if mode=='permissions':
        monkeypatch.setattr('royalbooks.bookorbit.os.link',lambda *a,**k:(_ for _ in ()).throw(PermissionError()))
    if mode=='write_permissions':
        from pathlib import Path
        original=Path.open
        def denied(path,mode='r',*args,**kwargs):
            if path.suffix=='.part' and mode=='xb':raise PermissionError()
            return original(path,mode,*args,**kwargs)
        monkeypatch.setattr(Path,'open',denied)
    if mode=='copy_changed':
        monkeypatch.setattr('royalbooks.bookorbit.shutil.copyfileobj',lambda inp,out:out.write(b'tampered'))
    deliver_staged(job,store,stage,inbox)
    assert store.get(job['id'])['status']=='needs_review'
    assert source.exists()
    if mode=='collision':assert next(inbox.glob('*.epub')).read_bytes()==b'do not overwrite'
    else:assert not list(inbox.glob('*.epub'))
    assert not list(inbox.glob('*.part'))


def test_restart_in_atomic_publication_window_never_reimports(tmp_path,monkeypatch):
    store,job,stage,inbox=staged(tmp_path)
    original=os.link
    class PowerLoss(BaseException):pass
    def interrupted(src,dest,**kwargs):
        original(src,dest,**kwargs)
        raise PowerLoss()
    monkeypatch.setattr('royalbooks.bookorbit.os.link',interrupted)
    store.update(job['id'],'delivering')
    with pytest.raises(PowerLoss):deliver_staged(job,store,stage,inbox)
    assert len(list(inbox.glob('*.epub')))==1
    recovered=JobStore(store.path)
    assert recovered.get(job['id'])['status']=='needs_review'
    deliver_staged(recovered.get(job['id']),recovered,stage,inbox)
    assert len(list(inbox.glob('*.epub')))==1
    assert Handoffs(store.path,inbox).describe(recovered.get(job['id']))['status']=='needs_review'


def test_same_bytes_from_another_source_cannot_publish_twice(tmp_path):
    store,job,stage,inbox=staged(tmp_path)
    payload=(stage/(job['id']+'.epub')).read_bytes()
    deliver_staged(job,store,stage,inbox)
    other=store.add(43)
    (stage/(other['id']+'.epub')).write_bytes(payload)
    store.update(other['id'],'staged',sha256=job['sha256'],file_size=len(payload))
    deliver_staged(store.get(other['id']),store,stage,inbox)
    assert store.get(other['id'])['error']=='duplicate_epub'
    assert len(list(inbox.glob('*.epub')))==1


@pytest.mark.asyncio
@pytest.mark.parametrize('mode',['missing','same_title_different_file','exact','dock_error','disabled'])
async def test_import_confirmation_requires_registered_identical_bytes(tmp_path,mode):
    store,job,stage,inbox=staged(tmp_path)
    payload=(stage/(job['id']+'.epub')).read_bytes()
    ledger=Handoffs(store.path,inbox);ledger.deliver(job,store,stage)
    next(inbox.glob('*.epub')).unlink()
    def handler(r):
        if r.url.path.endswith('/auth/login'):return httpx.Response(200,json=tokens())
        if r.url.path.endswith('/book-dock/files'):
            rows=[{'fileName':ledger.receipt(job['id'])['filename'],'status':'error'}] if mode=='dock_error' else []
            return httpx.Response(200,json={'items':rows,'total':len(rows)})
        if r.url.path.endswith('/books/query'):
            assert json.loads(r.content)['q']=='Die Seelenburg'
            return httpx.Response(200,json={'items':[] if mode=='missing' else [{'id':7}]})
        if r.url.path.endswith('/books/7'):return httpx.Response(200,json={'files':[{'id':10,'format':'epub'}]})
        return httpx.Response(200,content=payload if mode=='exact' else epub(title='Different'))
    client=Client(Config(url='http://bookorbit:3000',secret_file=secret(tmp_path),verify_import=mode!='disabled'),transport=httpx.MockTransport(handler))
    try:
        result=await ledger.verify(store.get(job['id']),client)
        expected='import_confirmed' if mode=='exact' else 'dock_error' if mode=='dock_error' else 'import_unconfirmed'
        assert result['status']==expected
        assert not list(inbox.glob('*.epub'))
    finally:await client.close()


def test_old_database_migrates_without_replaying_legacy_deliveries(tmp_path):
    path=str(tmp_path/'old.sqlite')
    with sqlite3.connect(path) as db:
        db.execute('CREATE TABLE jobs(id TEXT PRIMARY KEY,source TEXT,source_id INTEGER,status TEXT,error TEXT,sha256 TEXT,file_size INTEGER,created_at TEXT DEFAULT CURRENT_TIMESTAMP,updated_at TEXT DEFAULT CURRENT_TIMESTAMP,UNIQUE(source,source_id))')
        db.executemany('INSERT INTO jobs(id,source,source_id,status) VALUES(?,?,?,?)',
                       [('a'*32,'gutendex',1,'delivered'),('b'*32,'gutendex',2,'staged'),('c'*32,'gutendex',3,'queued')])
    store=JobStore(path)
    assert store.get('a'*32)['status']=='delivered'
    assert store.get('b'*32)['status']=='needs_review'
    assert store.get('c'*32)['v2_authorized']==0
    assert store.claim(('staged',),v2_only=True) is None
    assert Handoffs(path).describe(store.get('a'*32))['status']=='legacy_unverified'


def test_storage_write_probe_never_leaves_an_importable_file(tmp_path):
    data=tmp_path/'data';data.mkdir();inbox=tmp_path/'dock';inbox.mkdir()
    result=diagnose(data_path=str(data),ingest_path=str(inbox),write_test=True)
    assert any(c['name']=='bookorbit_write_probe' and c['status']=='ok' for c in result['checks'])
    assert not list(inbox.iterdir())
    bad=diagnose(data_path=str(data),ingest_path=str(inbox),library_path=str(inbox),write_test=True)
    assert bad['status']=='error'


@pytest.mark.asyncio
async def test_unreachable_server_is_reported_without_exception_details(tmp_path):
    def unavailable(request):
        raise httpx.ConnectError('private network detail must not escape',request=request)
    client=Client(Config(url='http://orbit:3000',secret_file=secret(tmp_path)),transport=httpx.MockTransport(unavailable))
    try:
        result=await client.connection()
        assert result['error']=='bookorbit_unavailable'
        assert not result['reachable'] and 'private network' not in str(result)
    finally:await client.close()


@pytest.mark.asyncio
async def test_file_download_renews_a_revoked_access_session(tmp_path):
    calls=[]
    def handler(r):
        calls.append(r.url.path)
        if r.url.path.endswith('/auth/login'):return httpx.Response(200,json=tokens())
        if r.url.path.endswith('/auth/refresh'):return httpx.Response(200,json=tokens('new-access'))
        if r.url.path.endswith('/books/7'):return httpx.Response(200,json={'files':[{'id':8,'format':'epub'}]})
        if r.headers.get('Authorization')=='Bearer access-one':return httpx.Response(401)
        return httpx.Response(200,content=epub())
    client=Client(Config(url='http://orbit:3000',secret_file=secret(tmp_path)),transport=httpx.MockTransport(handler))
    try:
        assert await client.asset(7,'epub')==epub()
        assert calls.count('/api/v1/auth/refresh')==1
    finally:await client.close()
