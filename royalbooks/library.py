"""Read-only Calibre bookshelf and bounded, library-contained file access."""
from pathlib import Path, PurePosixPath
import sqlite3
from royalbooks.database import connect


class LibraryUnavailable(Exception):
    pass


def _root(location):
    if not location:
        raise LibraryUnavailable('library_not_configured')
    try:
        root = Path(location).resolve(strict=True)
        metadata = (root/'metadata.db').resolve(strict=True)
        if not root.is_dir() or not metadata.is_file() or not metadata.is_relative_to(root):
            raise OSError()
        return root
    except (OSError, RuntimeError):
        raise LibraryUnavailable('library_unavailable') from None


def _db(root):
    db = connect((root/'metadata.db').as_uri()+'?mode=ro', uri=True, timeout=3)
    db.execute('PRAGMA query_only=ON')
    db.create_function('casefold', 1, lambda value: str(value or '').casefold())
    return db


def bookshelf(location, query='', sort='recent', limit=60, offset=0):
    root = _root(location)
    order = {'recent':'b.timestamp DESC,b.id DESC', 'title':'b.sort COLLATE NOCASE,b.id',
             'author':'b.author_sort COLLATE NOCASE,b.sort COLLATE NOCASE,b.id'}[sort]
    needle = '%'+query.casefold().replace('\\','\\\\').replace('%','\\%').replace('_','\\_')+'%'
    clause = "casefold(b.title || ' ' || COALESCE(b.author_sort,'')) LIKE ? ESCAPE '\\'"
    try:
        with _db(root) as db:
            total = db.execute('SELECT COUNT(*) FROM books').fetchone()[0]
            count = db.execute('SELECT COUNT(*) FROM books b WHERE '+clause,(needle,)).fetchone()[0]
            rows = db.execute('SELECT b.id,b.title,b.author_sort,b.timestamp FROM books b WHERE '+clause+
                              ' ORDER BY '+order+' LIMIT ? OFFSET ?', (needle,limit,offset)).fetchall()
            items=[]
            for book_id,title,authors,added in rows:
                formats=[r[0].upper() for r in db.execute('SELECT format FROM data WHERE book=? ORDER BY format',(book_id,))]
                items.append({'id':book_id,'title':title,'authors':authors or 'Autor unbekannt',
                              'added_at':added,'formats':formats,'cover_url':f'/api/library/{book_id}/cover',
                              'epub_url':f'/api/library/{book_id}/epub' if 'EPUB' in formats else None})
        return {'items':items,'total':total,'filtered_total':count,'offset':offset,'limit':limit,
                'has_more':offset+len(items)<count}
    except sqlite3.Error:
        raise LibraryUnavailable('library_unreadable') from None


def library_file(location, book_id, kind):
    root = _root(location)
    try:
        with _db(root) as db:
            row=db.execute('SELECT path FROM books WHERE id=?',(book_id,)).fetchone()
            if not row:
                return None
            directory=row[0]
            if not isinstance(directory,str) or '\\' in directory or ':' in directory:
                return None
            relative=PurePosixPath(directory)
            if relative.is_absolute() or '..' in relative.parts:
                return None
            if kind=='cover':
                name='cover.jpg'
            else:
                entry=db.execute("SELECT name FROM data WHERE book=? AND UPPER(format)='EPUB'",(book_id,)).fetchone()
                if not entry or not isinstance(entry[0],str) or any(c in entry[0] for c in '/\\:') or entry[0] in {'.','..'}:
                    return None
                name=entry[0]+'.epub'
            file=root.joinpath(*relative.parts,name).resolve(strict=True)
            if not file.is_relative_to(root) or not file.is_file():
                return None
            return file
    except sqlite3.Error:
        raise LibraryUnavailable('library_unreadable') from None
    except (OSError, RuntimeError):
        return None
