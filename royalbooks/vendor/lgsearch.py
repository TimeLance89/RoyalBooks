"""Bundled, MIT-licensed HTML result-table parser from libgenapp/libgen-app.

Copyright (c) 2026 Shmuel Gilbaum. Original source:
https://github.com/libgenapp/libgen-app/blob/main/lgsearch.py
License copied to royalbooks/vendor/LICENSE.libgenapp.
RoyalBooks uses only the HTML table parser and URL builder; no external CLI,
browser automation, CAPTCHA bypass, user credential scraping or downloads.
"""
#!/usr/bin/env python3
"""Headless LibGen search -> JSON.

Exists because `libgen-downloader -s` only runs as an interactive terminal UI
and dies without a TTY, so there is no way to search from a tool call.  This
hits the same libgen+ mirrors the CLI uses and prints results you can feed
straight back to `libgen-downloader -d <MD5>`.

Stdlib only, so it keeps working regardless of the node/npm situation.
"""

import argparse
import gzip
import io
import json
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from html.parser import HTMLParser

# The project publishes its live mirror list here; the CLI reads the same file
# at startup.  Reading it rather than hardcoding a host means a blocked domain
# self-heals on the next run instead of breaking this script.
CONFIG_URL = "https://raw.githubusercontent.com/obsfx/libgen-downloader/configuration/config.v3.json"

# Only used if the config fetch fails (no network to GitHub, rate limit, ...).
FALLBACK_MIRRORS = [
    "https://libgen.li/",
    "https://libgen.vg/",
    "https://libgen.gl/",
    "https://libgen.bz/",
    "https://libgen.la/",
]

UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
)

# Topic codes from the mirror's own search form.
TOPICS = {
    "l": "non-fiction",
    "f": "fiction",
    "r": "fiction-rus",
    "c": "comics",
    "m": "magazines",
    "a": "articles",
    "s": "standards",
}
DEFAULT_TOPICS = "lf"  # books; widen with --topics when the user wants more

# "Search in fields" checkboxes on the mirror's form (columns[]): which fields the query matches.
FIELDS = {
    "t": "title",
    "a": "author(s)",
    "s": "series",
    "y": "year",
    "p": "publisher",
    "i": "ISBN",
}
DEFAULT_FIELDS = "ta"  # title + author, the mirror's own default

# Result table columns, in the order the mirror emits them.
COL_TITLE, COL_AUTHOR, COL_PUBLISHER, COL_YEAR = 0, 1, 2, 3
COL_LANGUAGE, COL_PAGES, COL_SIZE, COL_EXT, COL_MIRRORS = 4, 5, 6, 7, 8

MD5_RE = re.compile(r"md5=([0-9a-fA-F]{32})")


def fetch(url, timeout):
    req = urllib.request.Request(
        url, headers={"User-Agent": UA, "Accept-Encoding": "gzip", "Accept": "*/*"}
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        raw = resp.read()
    if raw[:2] == b"\x1f\x8b":
        raw = gzip.GzipFile(fileobj=io.BytesIO(raw)).read()
    return raw.decode("utf-8", errors="replace")


def get_mirrors(explicit, timeout):
    if explicit:
        return [explicit if explicit.endswith("/") else explicit + "/"]
    try:
        cfg = json.loads(fetch(CONFIG_URL, timeout))
        mirrors = [m["src"] for m in cfg.get("mirrors", []) if m.get("src")]
        if mirrors:
            return mirrors
    except Exception as e:
        print(f"lgsearch: could not read mirror config ({e}); using fallback list",
              file=sys.stderr)
    return list(FALLBACK_MIRRORS)


class ResultTableParser(HTMLParser):
    """Pulls rows out of <table id="tablelibgen">.

    A real parser rather than regexes because the title cell carries tooltip
    attributes containing literal '<br>' text, which shreds any naive
    tag-stripping regex and leaks markup into the titles.
    """

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.rows = []
        self._in_table = False
        self._depth = 0          # tag depth inside the results table
        self._cells = None       # cells of the row being read
        self._cell = None        # {'text': [...], 'links': [...]}
        self._bold = 0           # <b> nesting; series/issue labels live in there
        self._anchor = None      # currently open <a>

    def handle_starttag(self, tag, attrs):
        a = dict(attrs)
        if tag == "table":
            if a.get("id") == "tablelibgen":
                self._in_table, self._depth = True, 0
            elif self._in_table:
                self._depth += 1
            return
        if not self._in_table:
            return
        if tag == "tr" and self._depth == 0:
            self._cells = []
        elif tag == "td" and self._cells is not None:
            self._cell = {"text": [], "links": []}
        elif tag == "b":
            self._bold += 1
        elif tag == "a" and self._cell is not None:
            self._anchor = {"href": a.get("href", ""), "text": [],
                            "bold": self._bold > 0}
        elif tag == "br" and self._cell is not None:
            self._cell["text"].append(" ")

    def handle_endtag(self, tag):
        if tag == "table" and self._in_table:
            if self._depth == 0:
                self._in_table = False
            else:
                self._depth -= 1
            return
        if not self._in_table:
            return
        if tag == "a" and self._anchor is not None:
            self._anchor["text"] = clean("".join(self._anchor["text"]))
            self._cell["links"].append(self._anchor)
            # Also fold link text into the cell: some columns (Size) put their
            # only content inside an <a>, so the cell would otherwise be empty.
            self._cell["text"].append(" " + self._anchor["text"] + " ")
            self._anchor = None
        elif tag == "b" and self._bold:
            self._bold -= 1
        elif tag == "td" and self._cell is not None:
            self._cell["value"] = clean("".join(self._cell["text"]))
            self._cells.append(self._cell)
            self._cell = None
        elif tag == "tr" and self._cells is not None:
            if len(self._cells) > COL_MIRRORS:
                self.rows.append(self._cells)
            self._cells = None

    def handle_data(self, data):
        if self._anchor is not None:
            self._anchor["text"].append(data)
        elif self._cell is not None:
            self._cell["text"].append(data)


def clean(s):
    return re.sub(r"\s+", " ", s).replace("\xa0", " ").strip()


def pick_title(cell):
    """The title cell holds series name, issue label and title.

    The series/issue parts sit inside <b>; the work's own title is the first
    edition link outside it.  Falling back to the longest link, then to the
    raw cell text, keeps odd rows from coming back empty.
    """
    editions = [l for l in cell["links"] if l["href"].startswith("edition.php")]
    plain = [l for l in editions if not l["bold"] and l["text"]]
    if plain:
        return plain[0]["text"]
    if editions:
        return max((l["text"] for l in editions), key=len, default="") or cell["value"]
    return cell["value"]


def row_to_entry(cells):
    md5 = ""
    for link in cells[COL_MIRRORS]["links"]:
        m = MD5_RE.search(link["href"])
        if m:
            md5 = m.group(1).lower()
            break
    if not md5:
        return None
    return {
        "md5": md5,
        "title": pick_title(cells[COL_TITLE]),
        "author": cells[COL_AUTHOR]["value"],
        "publisher": cells[COL_PUBLISHER]["value"],
        "year": cells[COL_YEAR]["value"],
        "language": cells[COL_LANGUAGE]["value"],
        "pages": cells[COL_PAGES]["value"],
        "size": cells[COL_SIZE]["value"],
        "extension": cells[COL_EXT]["value"].lower(),
        # relative edition-page link (e.g. "edition.php?id=123"); join with the mirror base
        "href": next((l["href"] for l in cells[COL_TITLE]["links"] if "edition.php" in l["href"]), ""),
    }


def get_details(md5, mirrors, timeout, verbose):
    """Full metadata for one MD5 via json.php.

    Worth using here and nowhere else: json.php is a keyed lookup, not a search
    endpoint, and it won't batch (comma lists are rejected, repeated md5= params
    return only the last).  For a whole result page that means ~2 requests per
    row to recover fields the search HTML already gave us in one -- but for a
    single chosen book it's one request for authoritative, unscraped data.
    """
    errors = []
    for mirror in mirrors:
        url = mirror + "json.php?" + urllib.parse.urlencode(
            {"object": "f", "md5": md5, "addkeys": "*"})
        if verbose:
            print(f"lgsearch: GET {url}", file=sys.stderr)
        try:
            payload = json.loads(fetch(url, timeout))
        except Exception as e:
            errors.append(f"{mirror}: {e}")
            continue
        if isinstance(payload, dict) and "error" in payload:
            errors.append(f"{mirror}: {payload['error']}")
            continue
        if payload:
            return payload, mirror, errors
        errors.append(f"{mirror}: no record for {md5}")
    return None, None, errors


def build_url(mirror, query, topics, page, per_page, fields=DEFAULT_FIELDS):
    params = [("req", query), ("res", str(per_page)), ("gmode", "on")]
    params += [("columns[]", c) for c in fields]       # which fields the query matches
    params.append(("objects[]", "f"))                  # files only: they carry MD5s; other objects
    #                                                    (editions/series/authors...) return a different
    #                                                    table with nothing downloadable
    params += [("topics[]", t) for t in topics]
    if page > 1:
        params.append(("page", str(page)))
    return mirror + "index.php?" + urllib.parse.urlencode(params)


def is_overloaded(html):
    """libgen.li answers HTTP 200 with a DB-overload notice fairly often
    ('max_user_connections'), so a 200 is not proof of a real result page.
    This is transient — worth retrying the same mirror before moving on."""
    return "alert-danger" in html and "tablelibgen" not in html


def search(query, topics, page, per_page, mirrors, timeout, retries, verbose, fields=DEFAULT_FIELDS):
    errors = []
    for mirror in mirrors:
        url = build_url(mirror, query, topics, page, per_page, fields)
        for attempt in range(1, retries + 1):
            if verbose:
                print(f"lgsearch: GET {url} (attempt {attempt}/{retries})", file=sys.stderr)
            try:
                html = fetch(url, timeout)
            except Exception as e:
                errors.append(f"{mirror}: {e}")
                break  # network-level failure: next mirror, not another attempt
            if is_overloaded(html):
                errors.append(f"{mirror}: database overloaded (attempt {attempt})")
                if attempt < retries:
                    time.sleep(2)
                    continue
                break
            parser = ResultTableParser()
            parser.feed(html)
            if not parser.rows and "tablelibgen" not in html:
                errors.append(f"{mirror}: no result table (blocked, captcha or layout change)")
                break
            entries = [e for e in (row_to_entry(r) for r in parser.rows) if e]
            return entries, mirror, errors
    return None, None, errors


PAGE_SIZE = 100   # the mirror's hard cap per page (asking for more silently drops to 25)
MAX_PAGES = 10    # ponytail: hard stop so a prolific author never becomes an unbounded crawl


def search_all(query, topics, mirrors, timeout, retries, verbose, fields=DEFAULT_FIELDS,
               want=25, page=1, ext=None, lang=None, min_size=None, max_size=None, max_pages=MAX_PAGES):
    """Page through results applying the client-side filters until `want` entries are collected,
    the last page is reached, or max_pages is hit. Sticks to the mirror that answered page 1.
    Returns (entries, mirror, errors, pages_fetched); entries is None if page 1 failed everywhere."""
    collected, errors, mirror_used, fetched = [], [], None, 0
    for n in range(max_pages):
        entries, mirror, errs = search(query, topics, page + n, PAGE_SIZE,
                                       [mirror_used] if mirror_used else mirrors,
                                       timeout, retries, verbose, fields)
        errors += errs
        if entries is None:
            if mirror_used is None:
                return None, None, errors, 0   # first page failed on every mirror
            break                              # a later page failed: keep what we have
        fetched, mirror_used = n + 1, mirror
        collected += apply_filters(entries, ext, lang, min_size, max_size)
        if len(collected) >= want or len(entries) < PAGE_SIZE:
            break                              # enough, or that was the last page
    return collected[:want], mirror_used, errors, fetched


def as_table(entries):
    if not entries:
        return "no results"
    head = ("#", "TITLE", "AUTHOR", "YEAR", "EXT", "SIZE", "MD5")
    rows = [
        (str(i), e["title"][:58], e["author"][:28], e["year"], e["extension"],
         e["size"], e["md5"])
        for i, e in enumerate(entries, 1)
    ]
    widths = [max(len(r[i]) for r in (head,) + tuple(rows)) for i in range(len(head))]
    fmt = "  ".join("{:<%d}" % w for w in widths)
    return "\n".join([fmt.format(*head)] + [fmt.format(*r) for r in rows])


def _size_to_mb(size_str):
    """Parse a size string like '14 MB' / '823 Kb' / '1.2 GB' into megabytes."""
    m = re.match(r"([\d.]+)\s*([KMG]?)B", size_str, re.IGNORECASE)
    if not m:
        return None
    value, unit = float(m.group(1)), m.group(2).upper()
    scale = {"": 1 / 1024, "K": 1 / 1024, "M": 1, "G": 1024}[unit]
    return value * scale


def apply_filters(entries, ext=None, lang=None, min_size=None, max_size=None):
    """Client-side result filtering shared by the CLI and libgen_app.py.
    Empty/None means "no filter"; sizes are in MB."""
    if ext:
        want = ext.lower().lstrip(".")
        entries = [e for e in entries if e["extension"] == want]
    if lang:
        want = lang.lower()
        entries = [e for e in entries if e["language"].lower() == want]
    if min_size or max_size:
        kept = []
        for e in entries:
            mb = _size_to_mb(e["size"])
            if mb is None:
                continue
            if min_size and mb < min_size:
                continue
            if max_size and mb > max_size:
                continue
            kept.append(e)
        entries = kept
    return entries


def main():
    p = argparse.ArgumentParser(
        description="Search LibGen mirrors headlessly and print results as JSON.",
        epilog="Feed a resulting md5 to: libgen-downloader -d <MD5>",
    )
    p.add_argument("query", nargs="?", help="search terms (title and/or author)")
    p.add_argument("--details", metavar="MD5",
                   help="skip search; print full json.php metadata for one MD5")
    p.add_argument("--limit", type=int, default=25,
                   help="max results (default 25); pages are fetched automatically until filled")
    p.add_argument("--page", type=int, default=1, help="result page (default 1)")
    p.add_argument("--ext", help="keep only this extension, e.g. pdf, epub, djvu")
    p.add_argument("--lang", help="keep only this language, e.g. English")
    p.add_argument("--min-size", type=float, metavar="MB",
                   help="keep only results at least this many MB")
    p.add_argument("--max-size", type=float, metavar="MB",
                   help="keep only results at most this many MB")
    p.add_argument("--topics", default=DEFAULT_TOPICS,
                   help="topic codes to search: " +
                        ", ".join(f"{k}={v}" for k, v in TOPICS.items()) +
                        f" (default {DEFAULT_TOPICS} = books; 'all' for everything)")
    p.add_argument("--fields", default=DEFAULT_FIELDS,
                   help="fields the query matches: " +
                        ", ".join(f"{k}={v}" for k, v in FIELDS.items()) +
                        f" (default {DEFAULT_FIELDS}; 'all' for everything)")
    p.add_argument("--mirror", help="force a specific mirror instead of the live list")
    p.add_argument("--timeout", type=int, default=45, help="per-request seconds")
    p.add_argument("--retries", type=int, default=3,
                   help="attempts per mirror when its database is overloaded (default 3)")
    p.add_argument("--table", action="store_true", help="human-readable table")
    p.add_argument("--json", action="store_true", help="JSON (default)")
    p.add_argument("-v", "--verbose", action="store_true", help="log requests to stderr")
    args = p.parse_args()

    if not args.query and not args.details:
        p.error("give a search query, or --details <MD5>")

    if args.details:
        md5 = args.details.strip().lower()
        if not re.fullmatch(r"[0-9a-f]{32}", md5):
            p.error(f"--details expects a 32-character MD5, got {args.details!r}")
        mirrors = get_mirrors(args.mirror, args.timeout)
        payload, mirror, errors = get_details(md5, mirrors, args.timeout, args.verbose)
        if payload is None:
            print(f"lgsearch: could not fetch details for {md5}:", file=sys.stderr)
            for e in errors:
                print(f"  - {e}", file=sys.stderr)
            return 2
        print(json.dumps({"md5": md5, "mirror": mirror, "details": payload},
                         indent=2, ensure_ascii=False))
        return 0

    topics = "".join(TOPICS) if args.topics == "all" else args.topics
    bad = [t for t in topics if t not in TOPICS]
    if bad:
        p.error(f"unknown topic code(s): {''.join(bad)}. Valid: {''.join(TOPICS)}")
    fields = "".join(FIELDS) if args.fields == "all" else args.fields
    bad = [f for f in fields if f not in FIELDS]
    if bad:
        p.error(f"unknown field code(s): {''.join(bad)}. Valid: {''.join(FIELDS)}")

    mirrors = get_mirrors(args.mirror, args.timeout)
    entries, mirror, errors, _ = search_all(args.query, topics, mirrors, args.timeout,
                                            max(1, args.retries), args.verbose, fields,
                                            want=args.limit, page=args.page, ext=args.ext,
                                            lang=args.lang, min_size=args.min_size,
                                            max_size=args.max_size)
    if entries is None:
        print("lgsearch: every mirror failed:", file=sys.stderr)
        for e in errors:
            print(f"  - {e}", file=sys.stderr)
        return 2


    if args.verbose:
        print(f"lgsearch: {len(entries)} result(s) from {mirror}", file=sys.stderr)

    if args.table:
        print(as_table(entries))
    else:
        print(json.dumps({"query": args.query, "mirror": mirror,
                          "count": len(entries), "results": entries},
                         indent=2, ensure_ascii=False))
    return 0 if entries else 1


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        sys.exit(130)
