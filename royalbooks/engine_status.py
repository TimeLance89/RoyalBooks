"""Localhost-only JDownloader observer. Exports no URLs, cookies or credentials."""
import asyncio
import json
import os
import re
import time
from pathlib import Path

import httpx


def summarize(packages,links):
    groups={}
    ids={p['uuid']:p['name'][11:] for p in packages
         if re.fullmatch(r'RoyalBooks-[a-f0-9]{32}',str(p.get('name','')))}
    for link in links:
        job=ids.get(link.get('packageUUID'))
        if job:groups.setdefault(job,[]).append(link)
    result={}
    for job,items in groups.items():
        texts=[str(x.get('name',''))+' '+str(x.get('status','')) for x in items]
        complete=any(x.get('finished') and x.get('bytesLoaded',0)>0 for x in items)
        moving=any(x.get('bytesLoaded',0)>0 and not x.get('finished') for x in items)
        blocked=all('cloudflare' in text.casefold() for text in texts)
        captcha=any('captcha' in text.casefold() for text in texts)
        failed=all(re.search(r'file not found|offline|error|failed',text,re.I) for text in texts)
        status=('finished_unverified' if complete else 'downloading' if moving else
                'blocked' if blocked else 'captcha' if captcha else 'failed' if failed else 'waiting')
        result[job]={'status':status,'bytes_loaded':sum(int(x.get('bytesLoaded') or 0) for x in items),
                     'bytes_total':sum(int(x.get('bytesTotal') or 0) for x in items)}
    return result


def read_status(root):
    try:
        path=root/'engine-state/status.json'
        if path.stat().st_size>512*1024 or path.is_symlink():return {}
        data=json.loads(path.read_text())
        if time.time()-data['checked_at']>30:return {}
        return data
    except (OSError,ValueError,KeyError,TypeError):return {}


async def main():
    target=Path(os.getenv('ROYALBOOKS_ENGINE_STATE_PATH','/state/status.json'))
    async with httpx.AsyncClient(timeout=5,trust_env=False,follow_redirects=False) as client:
        while True:
            result={'checked_at':int(time.time()),'reachable':False,'jobs':{}}
            try:
                query=json.dumps({'name':True,'status':True,'finished':True,'bytesLoaded':True,'bytesTotal':True})
                values=[]
                for endpoint in ['queryPackages','queryLinks']:
                    response=await client.get('http://127.0.0.1:3128/downloadsV2/'+endpoint,params={'query':query})
                    response.raise_for_status()
                    if len(response.content)>2*1024*1024:raise ValueError('engine_response_too_large')
                    payload=response.json()['data']
                    if not isinstance(payload,list):raise ValueError('engine_invalid_response')
                    values.append(payload)
                result.update(reachable=True,jobs=summarize(*values))
            except (httpx.HTTPError,ValueError,KeyError,TypeError):pass
            temp=target.with_suffix('.tmp')
            temp.write_text(json.dumps(result));os.replace(temp,target)
            await asyncio.sleep(5)


if __name__=='__main__':asyncio.run(main())
