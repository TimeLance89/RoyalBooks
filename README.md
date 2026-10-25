# RoyalBooks 2.0

Self-hosted ebook discovery and validated EPUB acquisition in one Docker
container. Open the web UI, enter a title, author or ISBN, and search. German is
the default. Prowlarr, Jackett, Torznab/Newznab, provider accounts and provider
API keys are **not required**.

## What actually works

- Independent built-in DNB, Open Library, Wikisource/WSIndex, Standard Ebooks,
  Gutendex and Library Genesis adapters; optional local Gutenberg CSV/RDF index.
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
| `ROYALBOOKS_CWA_LIBRARY_PATH` | Actual **host** Calibre library directory; mounted read-only in RoyalBooks. Used only for optional presence checks. |
| `ROYALBOOKS_DOWNLOADS_ENABLED=0` | Pauses automatic processing and new/retried jobs. Default is enabled for newly consented v2 jobs. |
| `ROYALBOOKS_ENABLE_LEGACY_WORKER=1` | Explicitly resumes historic backlog as well. Default 0 preserves it paused. |
| `GOOGLE_BOOKS_API_KEY` | Optional extra metadata source; never required. |

The CWA ingest folder and RoyalBooks storage must be separate. A `delivered`
status means handed to the ingest folder; it does not establish successful
Calibre import. An identical file found in the read-only library check proves
presence, not which job caused it. RoyalBooks never writes `metadata.db`.

Normal search is intended for a LAN. Protect Internet exposure with an
authenticating reverse proxy. Public search has a per-client quota; personal
history and management remain token-protected.

## Safe upgrade

Back up the SQLite database and current Compose/.env files before upgrading.
Migrations only add the `jobs.v2_authorized` column when absent. Existing books,
watchlist and job history are preserved. Historic queued/staged jobs are paused
unless the legacy flag is explicitly enabled. A newly confirmed request can
release the corresponding old queued job; retry remains limited to classified
transient errors and three total attempts. Ambiguous handoffs require review.

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
