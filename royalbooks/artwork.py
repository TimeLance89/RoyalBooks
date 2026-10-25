"""Only catalog-listed LibGen cover images, pinned origins and bounded JPEG cache."""
import re
import time
from urllib.parse import urlsplit
from royalbooks.database import connect
from royalbooks.libgen_links import CATALOG_HOSTS


def safe_artwork(md5,url):
    try:
        p=urlsplit(url)
        return (bool(re.fullmatch(r'[a-f0-9]{32}',md5)) and p.scheme=='https'
                and p.hostname in CATALOG_HOSTS and not p.username and not p.password
                and p.port in (None,443) and not p.query and not p.fragment
                and bool(re.fullmatch(r'/(?:covers|fictioncovers)/[0-9]+/'+md5+r'(?:_small)?\.jpg',p.path)))
    except (ValueError,TypeError):return False


def register_artwork(path,md5,url):
    if not safe_artwork(md5,url):return
    with connect(path,timeout=5) as db:
        db.execute('CREATE TABLE IF NOT EXISTS catalog_artwork (md5 TEXT PRIMARY KEY,url TEXT NOT NULL,touched INTEGER NOT NULL,content BLOB)')
        db.execute('INSERT INTO catalog_artwork VALUES(?,?,?,NULL) ON CONFLICT(md5) DO UPDATE SET url=excluded.url,touched=excluded.touched',(md5,url,int(time.time())))
        db.execute('DELETE FROM catalog_artwork WHERE md5 NOT IN (SELECT md5 FROM catalog_artwork ORDER BY touched DESC LIMIT 500)')


def read_artwork(path,md5):
    with connect(path,timeout=5) as db:
        try:row=db.execute('SELECT url,content FROM catalog_artwork WHERE md5=?',(md5,)).fetchone()
        except Exception:return None
    return row if row and safe_artwork(md5,row[0]) else None


def save_artwork(path,md5,content):
    with connect(path,timeout=5) as db:db.execute('UPDATE catalog_artwork SET content=? WHERE md5=?',(content,md5))


async def fetch_artwork(path,md5,client):
    import asyncio
    row=await asyncio.to_thread(read_artwork,path,md5)
    if not row:return None
    url,cached=row
    if cached:return bytes(cached)
    p=urlsplit(url)
    response=await client.get(url,headers={'Referer':f'https://{p.hostname}/'},follow_redirects=False)
    content=response.content
    if response.status_code!=200 or not 3<len(content)<=512*1024 or not content.startswith(b'\xff\xd8\xff'):
        return None
    await asyncio.to_thread(save_artwork,path,md5,content)
    return content
