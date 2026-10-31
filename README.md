# RoyalBooks 2.0

Self-hosted ebook discovery and validated EPUB acquisition in one Docker
container. Open the web UI, enter a title, author or ISBN, and search. German is
the default. Prowlarr, Jackett, Torznab/Newznab, provider accounts and provider
API keys are **not required**.

## What actually works

- Calibre bookshelf as the home screen, with original covers, local title/author
  search, recent/title/author sorting, grid/list view and EPUB downloads to the
  current device. Desktop sidebar and mobile bottom navigation separate library,
  discovery, watchlist and downloads. Light/dark and view preferences persist;
  credentials are never stored in browser storage.
- Independent built-in DNB, Open Library, Wikisource/WSIndex, Standard Ebooks,
  Gutendex, Library Genesis and Memory of the World adapters; optional local
  Gutenberg CSV/RDF index. LibGen filters EPUB and requested language upstream,
  with real 25-record pagination rather than stopping at the first short table.
- Parallel search with an eight-second provider deadline, six operation slots,
  bounded responses, short result cache, coalesced identical queries, refusal
  cooldowns and source-specific diagnostics. A failed source cannot erase
  results from healthy sources.
- DNB German editions/ISBNs; conservative work-language handling; grouped cards
  retain every source record and ISBN. Filename/catalog language is never file
  verification. Cover previews and descriptions appear when a source supplies them.
- Desktop/mobile detail dialog, source diagnostics, persistent watchlist,
  download center, storage preflight and read-only Calibre presence check.
- Source-bound EPUB jobs with persistent attempts, allowlisted redirects,
  32 MiB cap, ZIP/CRC/EPUB checks, SHA-256 deduplication and guarded CWA handoff.
  New jobs require a RoyalBooks administrator token and explicit rights consent.
  Ordinary search requires no token.

**Coverage is source-dependent.** DNB and Open Library are bibliographic sources,
not downloads. Wikisource has narrow German export coverage. Standard Ebooks is
English and its public feed contains only the latest 15 releases. LibGen returns
unverified format listings (EPUB/PDF/AZW3/etc.). Selected EPUBs now use a fresh,
source-listed GET link through its free download route, with pinned HTTPS
redirects, EPUB validation and catalog MD5 verification. Anna’s Archive and Z-Library have no
verified supported integration here. No CAPTCHA, access-control, DRM or paywall
bypass is implemented. PDF/AZW3 acquisition is not implemented.

See [source research](docs/source-research.md), [implementation plan](docs/royalbooks-2-plan.md)
and [test report](docs/test-report.md) for measured outcomes and limitations.

## Docker / NAS

```sh
docker compose build royalbooks
docker compose up -d royalbooks
```

The service runs as UID/GID **10001:10001**, with a read-only application
filesystem, writable `/data`, no Docker socket and no extra container. Existing
bind mounts and `.env` settings remain supported. The default bind address is
`127.0.0.1:8094`; set `ROYALBOOKS_BIND_IP` to your NAS LAN address when needed.

`ROYALBOOKS_DATA_PATH` defaults to `./data`. Its host directory must be writable
by UID 10001. Do not recursively change permissions on the Calibre library.

Optional settings:

| Variable | Meaning |
| --- | --- |
| `ROYALBOOKS_API_TOKEN` | Protects watchlist, queue, catalog imports and NAS diagnostics. Search works without it; downloads remain disabled without a token. Enter once in the administration panel and choose **Dieses Gerät anmelden**. A revocable HttpOnly session lasts seven days; the API token is never persisted in browser storage. |
| `ROYALBOOKS_CWA_INGEST_PATH` | Actual **host** directory mounted as `/cwa-book-ingest` in CWA. RoyalBooks mounts it as `/cwa-ingest`; UID 10001 needs write access. |
| `ROYALBOOKS_CWA_LIBRARY_PATH` | Actual **host** Calibre library directory; mounted read-only in RoyalBooks. Supplies the bookshelf, covers, existing EPUB files and optional presence checks. |
| `ROYALBOOKS_DOWNLOADS_ENABLED=0` | Pauses automatic processing and new/retried jobs. Default is enabled for newly consented v2 jobs. |
| `ROYALBOOKS_ENABLE_LEGACY_WORKER=1` | Explicitly resumes historic backlog as well. Default 0 preserves it paused. |
| `GOOGLE_BOOKS_API_KEY` | Optional extra metadata source; never required. |

The CWA ingest folder and RoyalBooks storage must be separate. A `delivered`
status means handed to the ingest folder; it does not establish successful
Calibre import. An identical file found in the read-only library check proves
presence, not which job caused it. RoyalBooks never writes `metadata.db`.

The authenticated `/api/library` endpoint lists existing Calibre books with
bounded pagination (`q`, `sort`, `limit`, `offset`). `/api/library/{id}/cover`
and `/api/library/{id}/epub` resolve only files registered inside that library;
metadata paths, directory traversal and symlinks cannot expose outside files.
An EPUB can be downloaded and opened with the device's reading app. An in-browser
EPUB reader, reading progress sync and Kindle delivery are not implemented.

Search returns 30 records per source by default, with an optional 60-record
selection. The known author misspelling "Steven King" is normalized to "Stephen
King" and visibly reported. Other misspellings are not silently rewritten.
Memory of the World is an independent community library with mostly scholarly,
English-language material; declared German/English language filters remain
strict. Only source-listed HTTPS EPUB paths under its pinned `/files/` origin
are eligible for the normal validated transfer and CWA handoff.

Normal search is intended for a LAN. Protect Internet exposure with an
authenticating reverse proxy. Public search has a per-client quota; personal
history and management remain token-protected.

## Safe upgrade

Back up the SQLite database and current Compose/.env files before upgrading.
Job migrations add `v2_authorized` and `retry_at` when absent. Existing books,
watchlist and job history are preserved. Historic queued/staged jobs are paused
unless the legacy flag is explicitly enabled. A newly confirmed request can
release the corresponding old queued job; retry remains limited to classified
transient errors and three total attempts. Ambiguous handoffs require review.

Authorized downloads automatically retry transient network/server failures after
60 and 300 seconds, with at most three total job attempts. The next retry time is
stored in SQLite and shown in Downloads; waiting retries do not block other books.
LibGen transfers try the four pinned catalog mirrors for the same MD5, obtaining
a fresh source-issued key from each. Refusals, throttling, login redirects and
untrusted destinations stop that mirror sequence. Missing files and invalid EPUBs
are not reported as successes; every successful transfer still passes the full
EPUB/CRC and source MD5 checks. Mirrors may share the same upstream outage, so this
does not guarantee availability for every listed book.

No automatic bulk Gutenberg import runs on startup. The optional catalog
controls download official CSV/RDF feeds only after an administrator requests
it. Existing imported data is searched immediately. No search-site scraping is
used for Gutenberg.

Optional indexer endpoints remain at `/api/indexers/search`; server-side Prowlarr
variables and `/data/indexers.json` still work. They are not used by primary
search. Existing `compose.indexer.yaml` overrides can remain in place.

## Verification

```sh
pip install -e '.[test]'
python -m pytest -q
python -m royalbooks.source_audit                     # offline dry run
python -m royalbooks.source_audit --live              # actual catalog evidence
python -m royalbooks.source_audit --live --download-publisher-epub
pip install -e '.[browser]'
python -m playwright install chromium
python scripts/browser_smoke.py                     # desktop/mobile, explicit fixtures
python scripts/nas_cwa_audit.py                      # read-only actual Docker mounts
```

The live audit uses an isolated temporary database. Its optional file checks
use publisher-listed historical public-domain titles; they do not write to the
production queue or Calibre library. Network refusals are reported as failures.
Reproducing a run later may yield different results as catalogs and hosts change.
GitHub Actions runs the full Linux suite, real browser checks, image build,
non-root package checks and actual container startup.

The existing MIT parser attribution is in `royalbooks/vendor/LICENSE.libgenapp`.

## Free LibGen acquisition

LibGen EPUB candidates now have an **EPUB übernehmen** action and are included
in **EPUB-Dateieinträge**, even though their short-lived download URL is resolved
only when a job runs. No provider account, payment or user-entered provider key
is required. RoyalBooks administration and per-job rights consent remain protected.

Search identities remain MD5-based; acquisition uses separate server-bound IDs.
Transient GET keys are read from actual source pages, never invented, persisted
or exposed to the browser. Only pinned HTTPS catalog/CDN hosts are accepted.
An access denial is respected. HTML-layout changes and source outages can still
prevent a transfer; this is not a guarantee of universal archive availability.

See [the LibGen follow-up](docs/libgen-acquisition.md) for actual file-transfer
evidence and the distinction between search candidates and verified files.

## Phone and browser access

Signing in on a desktop does not sign in a phone. Each device now has its own
revocable seven-day session, retained across page reloads and container restarts.
The API token stays on the server after sign-in; browser JavaScript cannot read
the session cookie. Logout revokes that device, and API-token rotation revokes
all sessions. Cookie-authenticated writes require the same-origin web header;
cross-origin requests are rejected. Existing `X-API-Key` clients still work.

An administrator with SSH access can issue a one-use, 24-hour device link:
`SessionStore(db_path).create_device_link(api_token)` from `royalbooks.auth`.
Open `/connect#code=...` on the intended device. The fragment is cleared before
exchange, never reaches HTTP access logs, and cannot be reused by another device.
No public endpoint creates these links. The actual phone must open its own link;
filling a token in an assistant's browser is not a phone login.

Optional private-network management access: `ROYALBOOKS_TRUSTED_NETWORKS` accepts
comma-separated peer CIDRs. Matching devices can use watchlist and downloads
without entering a token or retaining a cookie, including after server restarts.
The server API token remains required and is never sent to the browser. This
setting grants management access to every matching peer; use only for a private
LAN/VPN deployment. Forwarded headers do not grant access; same-origin write
checks remain enforced. Leave empty to require the normal device login.

## Automatic discovery storefront

The default home page is a browsing storefront with five independently cached
shelves: current-year German EPUB editions, recent catalog uploads, fiction,
nonfiction and actual Open Library daily trends. Current publication year and
recent upload are explicitly distinct. International work trends are never
marked as German editions or downloadable files; their action searches actual
EPUB sources. Store rows deduplicate repeated editions and omit obvious filename
metadata. Raw search records remain available unchanged.

Snapshots are persisted in RoyalBooks SQLite and refreshed on startup and hourly
by a cancellable background task. A failed/empty upstream response retains the
last successful shelf and its timestamp. `/api/store` serves the snapshot without
waiting on upstream calls. Background refresh does not enqueue downloads.
`ROYALBOOKS_STORE_REFRESH_ENABLED=0` disables refresh for offline deployments/tests.

LibGen's cover-enabled result tables supply explicit cover links and ISBNs. A
pinned, source-listed JPEG proxy caches at most 500 images, each no larger than
512 KiB. It uses the catalog origin's normal Referer and refuses redirects,
foreign origins, non-JPEG bodies and unregistered paths. No token or source
session is sent to the browser. Store and search previews use these cached covers.

Book details show source descriptions when present; otherwise a selected exact
ISBN or Open Library work can retrieve a bounded, cached Open Library synopsis.
Work descriptions are labeled. No synopsis or bestseller rank is fabricated.
The personal bookshelf also exposes descriptions already present in Calibre's
comments table, read only.

## RoyalBooks platform sections

RoyalDownloader's public navigation/settings structure was used as the product
reference, adapted to books with an author/work area and a separate settings
console. Main sections have independent reloadable URLs: `/discover`, `/search`,
`/authors`, `/library`, `/downloads`, `/watchlist`, `/settings/general`,
`/settings/sources`, `/settings/library`, `/settings/system` and `/settings/access`.
Old hash links remain usable. Browser back/forward navigation is supported.

Search modes are all fields, title, author and ISBN/EAN. Author mode uses actual
LibGen author columns, DNB person fields and Open Library author queries; results
only about an author are excluded. Works can be paged instead of being limited
to a single result batch. ISBN-10/13 and book-EAN checksums are validated; ISBN,
EAN and the common IAN label are understood. A catalog cannot guarantee a
complete bibliography; the author page reports loaded works and provides paging.

The source chooser persists selected catalogs and file providers, preferred
language, result count and the default reachable-only view in SQLite. Display
appearance is saved for the current browser. Connections expose status only,
without publishing tokens or NAS paths. Disabled sources are excluded from new
searches and store refreshes. Thalia's official free-book section is linked as
an external catalog, rather than being presented as an automatic free file API.

The default storefront/search/author views offer only files with a recent
reachability result. A separate background service deduplicates checks, limits
parallelism to three, prioritizes user searches over store checks, and caches
positive results for 15 minutes and negative results for two minutes. Probes use
only already indexed source-bound IDs, preserve the selected MD5 across pinned
redirects, request an initial range and stop after the ZIP prefix. Login HTML,
foreign redirects, wrong MD5s and oversized files are rejected. This is a
reachability check; full ZIP/CRC/EPUB/MD5 verification still occurs on download.
No probe adds a download job or imports a book. Optional catalog browsing can
show other works, with download buttons disabled until their file is reachable.

ISBN/EAN resolution can use an exact bibliographic match to find an EPUB of the
same work when the supplied ISBN identifies a different edition. That fallback
requires matching normalized title, author and language, keeps the EPUB's own
identifiers and visibly labels it as another edition; it never assigns a paper
ISBN to an unrelated download. Store refreshes also schedule bounded background
reachability checks, so frequently visited shelves are ready without a fresh
file check on every click.


## Genre browsing

Open **Genres** in the book kiosk to browse nine categories. `/genres` offers
language selection, subsequent pages, reloadable category URLs and an optional
reachable-EPUB filter. Open Library subject queries and Gutendex topic filters
supply actual catalogue classifications. A work with a German edition is not
relabeled as a verified German EPUB. Existing source-listed file editions are
attached only when normalized title and author match, and their own identifiers
and language are retained. Downloads still require a successful reachability
check and the existing complete-file validation.

Results are cached for six hours in SQLite and survive source outages. Disabled
sources are excluded, including from cache keys. Genre metadata APIs cannot
supply arbitrary origins or file URLs to the acquisition queue. References:
[Open Library Search API](https://openlibrary.org/dev/docs/api/search) and
[Gutendex topic filters](https://github.com/garethbjohnson/gutendex).

Actual failed downloads now invalidate the corresponding reachability record;
a previous successful range probe must not keep a failed file advertised.
Additional acquisition providers are enabled only after successful source/file
tests. An NAS audit of Anna's Archive `.gl`, `.pk` and `.gd` returned HTTP 403 on
all three searches after their normal redirects; no automatic provider was added.


## Byte.to and free hosters

The new `/hosters` area browses Byte.to and uses a separate optional JDownloader
engine for source-listed link containers, with persistent job state, live engine
observations, validated EPUB handoff and manual recovery after normal browser
verification. No premium or MyJDownloader account is required. Cloudflare/captcha
checks can still require human interaction; they are not bypassed. See
[setup, verified behavior and current limits](docs/hoster-downloads.md).
