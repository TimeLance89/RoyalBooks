"""Display metadata for queue entries; no secrets or download keys."""
import json
from royalbooks.database import connect

def remember(path,items):
    with connect(path,timeout=5) as db:
        db.execute('CREATE TABLE IF NOT EXISTS book_labels(source TEXT NOT NULL,record_id INTEGER NOT NULL,payload TEXT NOT NULL,touched INTEGER NOT NULL DEFAULT(unixepoch()),PRIMARY KEY(source,record_id))')
        for item in items:
            record=item.get('acquisition_id') or item.get('source_id')
            if not item.get('download_supported') or not str(record).isdigit():continue
            value={key:item.get(key) for key in ['title','authors','cover_url','year','isbns']}
            db.execute('INSERT INTO book_labels(source,record_id,payload) VALUES(?,?,?) ON CONFLICT(source,record_id) DO UPDATE SET payload=excluded.payload,touched=unixepoch()', (item['source'],int(record),json.dumps(value)))
        db.execute('DELETE FROM book_labels WHERE rowid NOT IN (SELECT rowid FROM book_labels ORDER BY touched DESC LIMIT 4000)')

def apply_labels(path,jobs):
    with connect(path,timeout=5) as db:
        exists=db.execute("SELECT 1 FROM sqlite_master WHERE name='book_labels'").fetchone()
        if not exists:return jobs
        for job in jobs:
            row=db.execute('SELECT payload FROM book_labels WHERE source=? AND record_id=?',(job['source'],job['source_id'])).fetchone()
            if row:job['book']=json.loads(row[0])
    return jobs
