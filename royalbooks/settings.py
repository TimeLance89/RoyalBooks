"""Persistent platform preferences; credentials and host paths remain server-side."""
import json
from royalbooks.database import connect

DEFAULTS={'language':'de','available_only':True,'result_limit':30,'sources':['webshare','byte','libgen','memoryoftheworld','dnb','openlibrary','wikisource','standardebooks','gutendex','gutenberg_catalog']}

class PlatformSettings:
    def __init__(self,path):
        self.path=path
        with connect(path,timeout=5) as db:db.execute('CREATE TABLE IF NOT EXISTS platform_settings (id INTEGER PRIMARY KEY CHECK(id=1),payload TEXT NOT NULL)')
    def get(self):
        with connect(self.path,timeout=5) as db:row=db.execute('SELECT payload FROM platform_settings WHERE id=1').fetchone()
        return {**DEFAULTS,**(json.loads(row[0]) if row else {})}
    def save(self,values):
        result={**self.get(),**values}
        with connect(self.path,timeout=5) as db:db.execute('INSERT INTO platform_settings VALUES(1,?) ON CONFLICT(id) DO UPDATE SET payload=excluded.payload',(json.dumps(result),))
        return result
