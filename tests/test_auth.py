"""Device handoff, reload-safe sessions, revocation and cookie write protection."""
from concurrent.futures import ThreadPoolExecutor

import httpx
import pytest

from royalbooks.app import app, _search_windows
from royalbooks.auth import SessionStore, COOKIE, SESSION_SECONDS
from royalbooks.watchlist import WatchlistStore


def test_store_keeps_only_hashes_and_survives_restart(tmp_path):
    path = str(tmp_path/'db.sqlite3')
    store = SessionStore(path)
    session = store.create_session('private-server-key')
    code = store.create_device_link('private-server-key')
    assert SessionStore(path).authenticated(session, 'private-server-key')
    assert not store.authenticated(session, 'rotated-server-key')
    raw = (tmp_path/'db.sqlite3').read_bytes()
    assert all(value.encode() not in raw for value in [session, code, 'private-server-key'])
    store.logout(session)
    assert not store.authenticated(session, 'private-server-key')


def test_expiry_and_single_use_are_enforced(tmp_path, monkeypatch):
    import royalbooks.auth as module
    store = SessionStore(str(tmp_path/'db.sqlite3'))
    session = store.create_session('key')
    code = store.create_device_link('key')
    with ThreadPoolExecutor(max_workers=2) as pool:
        outcomes = list(pool.map(lambda _: store.consume_device_link(code, 'key'), [1, 2]))
    assert sorted(outcomes) == [False, True]
    now = module.time.time()
    expired_code = store.create_device_link('key')
    monkeypatch.setattr(module.time, 'time', lambda: now + SESSION_SECONDS + 1)
    assert not store.authenticated(session, 'key')
    assert not store.consume_device_link(expired_code, 'key')
    assert not store.consume_device_link(store.create_device_link('key'), 'wrong-key')


def test_device_link_still_works_after_an_hour_but_expires_later(tmp_path, monkeypatch):
    import royalbooks.auth as module
    store = SessionStore(str(tmp_path/'db.sqlite3'))
    first = store.create_device_link('key')
    second = store.create_device_link('key')
    now = module.time.time()
    monkeypatch.setattr(module.time, 'time', lambda: now + 3600)
    assert store.consume_device_link(first, 'key')
    monkeypatch.setattr(module.time, 'time', lambda: now + 2*24*3600)
    assert not store.consume_device_link(second, 'key')


@pytest.fixture
def access(monkeypatch, tmp_path):
    _search_windows.clear()
    key = 'fixture-private-key-not-in-response'
    monkeypatch.setenv('ROYALBOOKS_API_TOKEN', key)
    path = str(tmp_path/'db.sqlite3')
    store = SessionStore(path)
    monkeypatch.setattr(app.state, 'sessions', store, raising=False)
    monkeypatch.setattr(app.state, 'watchlist', WatchlistStore(path), raising=False)
    return store, key


@pytest.mark.asyncio
async def test_login_cookie_can_read_and_write_without_exposing_api_key(access):
    _, key = access
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://test') as client:
        assert (await client.get('/api/watchlist')).status_code == 401
        headers = {'X-RoyalBooks-Client':'web', 'Origin':'http://test'}
        result = await client.post('/api/auth/login', json={'token':key}, headers=headers)
        assert result.status_code == 200
        cookie = result.headers['set-cookie']
        assert 'HttpOnly' in cookie and 'SameSite=strict' in cookie and key not in cookie
        assert key not in result.text
        assert (await client.get('/api/auth/status')).json()['authenticated']
        assert (await client.get('/api/watchlist')).status_code == 200
        book = {'source':'dnb','source_id':'12345','title':'Beispiel'}
        assert (await client.post('/api/watchlist', json=book)).status_code == 403
        assert (await client.post('/api/watchlist', json=book,
                headers={**headers,'Origin':'https://evil.example'})).status_code == 403
        assert (await client.post('/api/watchlist', json=book,headers=headers)).status_code == 201
        assert (await client.post('/api/auth/logout',headers=headers)).status_code == 200
        assert (await client.get('/api/watchlist')).status_code == 401


@pytest.mark.asyncio
async def test_device_link_logs_in_only_one_device(access):
    store, key = access
    code = store.create_device_link(key)
    headers = {'X-RoyalBooks-Client':'web'}
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://test') as first:
        assert (await first.post('/api/auth/connect',json={'code':code},headers=headers)).status_code == 200
        assert (await first.get('/api/watchlist')).status_code == 200
        assert (await first.post('/api/auth/connect',json={'code':code},headers=headers)).status_code == 200
        assert (await first.post('/api/auth/connect',json={'code':'x'*43},headers=headers)).status_code == 200
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://test') as second:
        assert (await second.post('/api/auth/connect',json={'code':code},headers=headers)).status_code == 401
        assert (await second.get('/api/auth/status')).json() == {'authenticated':False}


@pytest.mark.asyncio
async def test_https_cookie_is_secure_and_login_refuses_foreign_origin(access):
    _, key = access
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='https://test') as client:
        assert (await client.post('/api/auth/login',json={'token':key})).status_code == 403
        assert (await client.post('/api/auth/login',json={'token':key},
            headers={'X-RoyalBooks-Client':'web','Origin':'https://evil.example'})).status_code == 403
        response = await client.post('/api/auth/login',json={'token':key},headers={'X-RoyalBooks-Client':'web'})
        assert 'Secure' in response.headers['set-cookie']

@pytest.mark.asyncio
async def test_explicit_network_access_survives_without_cookie_and_blocks_foreign_writes(access, monkeypatch):
    monkeypatch.setenv('ROYALBOOKS_TRUSTED_NETWORKS', '192.168.178.0/24')
    headers = {'X-RoyalBooks-Client':'web', 'Origin':'http://test'}
    for _ in range(2):
        transport = httpx.ASGITransport(app=app, client=('192.168.178.25', 1234))
        async with httpx.AsyncClient(transport=transport, base_url='http://test') as client:
            status = await client.get('/api/auth/status')
            assert status.json() == {'authenticated':True, 'network_access':True}
            assert 'set-cookie' not in status.headers
            assert (await client.get('/api/watchlist')).status_code == 200
            book = {'source':'dnb','source_id':'12345','title':'Beispiel'}
            assert (await client.post('/api/watchlist',json=book)).status_code == 403
            assert (await client.post('/api/watchlist',json=book,headers={**headers,'Origin':'http://evil.example'})).status_code == 403
    transport = httpx.ASGITransport(app=app, client=('203.0.113.25', 1234))
    async with httpx.AsyncClient(transport=transport,base_url='http://test') as client:
        assert (await client.get('/api/watchlist',headers={'X-Forwarded-For':'192.168.178.25'})).status_code == 401
    monkeypatch.setenv('ROYALBOOKS_TRUSTED_NETWORKS', 'invalid')
    transport = httpx.ASGITransport(app=app, client=('192.168.178.25', 1234))
    async with httpx.AsyncClient(transport=transport,base_url='http://test') as client:
        assert (await client.get('/api/watchlist')).status_code == 401
