"""Reading-list persistence, authentication, and duplicate handling."""
import httpx
import pytest

from royalbooks.app import app
from royalbooks.watchlist import MAX_ENTRIES, WatchlistFull, WatchlistStore


def demo():
    return {
        "source": "openlibrary", "source_id": "OL123W",
        "title": "Die Verwandlung", "authors": ["Franz Kafka"],
        "language": "de", "info_url": "https://openlibrary.org/works/OL123W",
    }


def test_watchlist_persists_duplicates_and_deletions(tmp_path):
    path = str(tmp_path / "royalbooks.sqlite3")
    store = WatchlistStore(path)
    first = store.add(**demo())
    assert first["authors"] == ["Franz Kafka"]
    assert store.add(**demo())["id"] == first["id"]
    assert WatchlistStore(path).list()[0]["id"] == first["id"]
    assert store.remove(first["id"]) is True
    assert store.remove(first["id"]) is False
    assert store.list() == []


def test_watchlist_limit_enforced_in_sqlite_transaction(tmp_path, monkeypatch):
    import royalbooks.watchlist as module
    monkeypatch.setattr(module, "MAX_ENTRIES", 1)
    store=WatchlistStore(str(tmp_path/"db.sqlite3"))
    store.add(**demo())
    with pytest.raises(WatchlistFull):
        store.add(**{**demo(), "source_id": "OL345W"})


@pytest.mark.asyncio
async def test_watchlist_api_protected_validated_and_restart_safe(monkeypatch, tmp_path):
    token = "demo-token-secret-with-at-least-24-chars"
    monkeypatch.setenv("ROYALBOOKS_API_TOKEN", token)
    store=WatchlistStore(str(tmp_path/"db.sqlite3"))
    app.state.watchlist=store
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),base_url="http://test") as client:
        assert (await client.get("/api/watchlist")).status_code == 401
        assert (await client.post("/api/watchlist",json=demo())).status_code == 401
        headers={"X-API-Key":token}
        bad={**demo(),"source":"my_invalid_provider"}
        assert (await client.post("/api/watchlist",json=bad,headers=headers)).status_code == 422
        assert (await client.post("/api/watchlist",json={**demo(),"info_url":"javascript:alert(1)"},headers=headers)).status_code == 422
        first=await client.post("/api/watchlist",json=demo(),headers=headers)
        assert first.status_code == 201
        ident=first.json()["item"]["id"]
        again=await client.post("/api/watchlist",json=demo(),headers=headers)
        assert again.status_code == 201
        assert again.json()["item"]["id"] == ident
        response=await client.get("/api/watchlist",headers=headers)
        assert len(response.json()["items"]) == 1
        assert response.json()["items"][0]["title"] == "Die Verwandlung"
        assert (await client.delete("/api/watchlist/"+ident)).status_code == 401
        assert (await client.delete("/api/watchlist/"+ident,headers=headers)).status_code == 200
        assert (await client.delete("/api/watchlist/"+ident,headers=headers)).status_code == 404


@pytest.mark.asyncio
async def test_missing_api_token_fails_closed(monkeypatch,tmp_path):
    monkeypatch.delenv("ROYALBOOKS_API_TOKEN",raising=False)
    app.state.watchlist=WatchlistStore(str(tmp_path/"db.sqlite3"))
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),base_url="http://test") as client:
        assert (await client.get("/api/watchlist",headers={"X-API-Key":"guess"})).status_code == 503
