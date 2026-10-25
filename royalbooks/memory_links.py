"""Pinned, source-listed Memory of the World file paths."""
import re
from urllib.parse import quote, unquote, urlsplit

ORIGIN='https://library.memoryoftheworld.org'


def safe_memory_url(url, extension='epub'):
    if not isinstance(url,str) or len(url)>1200:
        return False
    try:
        p=urlsplit(url)
        if (p.scheme!='https' or p.hostname!='library.memoryoftheworld.org' or p.username
                or p.password or p.port not in (None,443) or p.query or p.fragment):
            return False
        path=unquote(p.path)
        if '%' in path or '\\' in path or any(ord(c)<32 for c in path):
            return False
        parts=path.split('/')
        return (len(parts)>=5 and parts[0]=='' and parts[1]=='files'
                and bool(re.fullmatch(r'[A-Za-z0-9_-]{1,80}',parts[2]))
                and all(part not in {'','.','..'} for part in parts[3:])
                and path.lower().endswith('.'+extension))
    except ValueError:
        return False


def listed_url(library_url, directory, filename, extension='epub'):
    if not all(isinstance(value,str) for value in [library_url,directory,filename]):
        return None
    if not library_url.startswith('/files/') or any(c in filename for c in '/\\'):
        return None
    path=library_url.rstrip('/')+'/'+(directory.strip('/')+'/' if directory.strip('/') else '')+filename
    url=ORIGIN+quote(path,safe='/')
    return url if safe_memory_url(url,extension) else None
