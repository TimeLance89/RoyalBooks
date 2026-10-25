# RoyalBooks 2.0 — acceptance report

Date: **2026-10-25**. Branch: `feature/royalbooks-2-integrated-providers`.
Base main: `81db3f1b7a59544f1849466ba60927113226a508`.

## Executed checks

| Check | Measured outcome |
| --- | --- |
| Full Windows/Python 3.11 suite | **169 passed, 1 skipped**. The skipped symlink test requires a Windows privilege absent on this account; it runs successfully in Linux. |
| Full NAS Docker/Python 3.12 suite | **170 passed**, running as UID 10001, source bind read-only, test dependencies and temporary files confined to `/tmp`. |
| Real Chromium, desktop 1440×1000 and mobile 390×844 | Both flows passed: search without token, grouped results, retained ISBNs, detail dialog/Escape, actual SQLite watchlist save/remove, filters, 503 recovery, no horizontal overflow or JavaScript errors. Provider responses in this repeatable smoke test are explicitly fixtures. |
| Actual NAS browser search | Both viewports passed against the deployed service with **real** results: “Harry Potter und der Feuerkelch” returned 21 source records grouped into 8 cards, including 10 unverified file listings. No JavaScript errors or overflow. |
| Live source audits | Four fixed queries, seven built-ins, both local host and NAS. Real bibliographic/file-listing results, including German records, were returned. Reports preserve source-specific outcomes. |
| Docker build | Successful on the Ugreen NAS; final candidate image `sha256:f58171fcd952fb5504f5899375325c8d733eb1df618838531349f5e57cdbc437`. |
| Docker runtime | Existing single RoyalBooks container recreated, version 2.0.0, healthy, UID/GID 10001:10001. Existing data volume retained. No Prowlarr dependency. |
| Security/integrity regressions | Included in full suite: URL/redirect allowlists, SSRF rejection, unsafe archives, CRC corruption, duplicate files, bounded responses, authentication, query escaping, isolated failures/deadlines, refusal cooldown, safe handoff and read-only library checks. |
| Database migration | Legacy-schema fixture preserves old queued jobs with zero consumed attempts; newly consented jobs are processed independently. Actual NAS had 0 jobs and 0 watchlist entries before upgrade; original SQLite snapshot retained before migration. |

Reproduce:

```sh
pip install -e '.[test,browser]'
python -m pytest -q
python -m playwright install chromium
python scripts/browser_smoke.py
python -m royalbooks.source_audit --live --output data/source-audit.json
python -m royalbooks.source_audit --live --download-publisher-epub
docker compose build royalbooks
docker compose up -d royalbooks
python scripts/nas_cwa_audit.py --json
```

On Windows use `PYTHONUTF8=1` for historic tests which omit explicit file encoding.
CI additionally builds the image and starts the actual non-root service with no
indexer variables, checking health, registry and persistent SQLite access.

## Source evidence

Raw measurements: [local audit](source-audit-local.json),
[NAS audit](source-audit-nas.json). Each contains the timestamp, query, source,
duration, error, declared language, ISBN and listed formats.

| Source | Measured capabilities and limits |
| --- | --- |
| DNB | Returned German edition metadata and ISBNs for Faust, Kafka and Don Kichote. No file offering. |
| Open Library | Returned work metadata, including some single-language German work records; multilingual work records remain ambiguous and are not German edition proof. |
| Gutendex | Actual `/books/` endpoint returned German results for Faust and Kafka, with publisher EPUB listings. Some separate requests timed out. The old slashless endpoint redirected and is corrected. |
| Wikisource | “Don Kichote” returned a German publisher-listed EPUB export. Narrow coverage; sampled German catalog had 12 rows including multilingual records. Actual export download returned **HTTP 503**, recorded as failure. |
| Standard Ebooks | “Suspense” returned a real English EPUB enclosure. Downloaded **781,966 bytes**, validated ZIP/EPUB/CRC and SHA-256. Latest-15 feed only, not a full English catalog and no German coverage. |
| Library Genesis | Real German file listings found. Observed listing formats: EPUB, PDF, AZW3, MOBI. A separate initial incompatible response was refused, not reported as a successful search. The initial audit did not download LibGen files. Follow-up: actual free-route EPUB transfer and catalog-MD5 validation passed; see [LibGen acquisition](libgen-acquisition.md). |
| Local Gutenberg | Adapter operates independently, but actual NAS catalog was not imported; returned no results. Offline import/RDF/search/acquisition fixtures pass. No claim of live full-catalog coverage. |
| Google Books | Optional existing adapter tested with fixtures; no live test without credentials. Not required for core search. |
| Anna’s Archive / Z-Library | Compared through public upstream documentation and implementation research. No stable supported anonymous download path established. Not implemented as fake working providers; no account, CAPTCHA, challenge or download test attempted. |

The twelve examined projects, licenses, selected files and adopted architectural
ideas are documented in [source research](source-research.md). New implementation
uses their concepts, not copied code. Existing attributed MIT parser retained.

## Actual NAS → Calibre acceptance

Evidence: [NAS acquisition record](nas-acceptance.json).

1. Confirmed actual RoyalBooks/CWA mounts. RoyalBooks previously used disabled
   placeholder mounts; configured the actual CWA ingest and read-only library.
2. Granted UID 10001 `rwx` **only on the ingest directory** through a named ACL.
   No recursive chmod/chown and no Calibre-library permissions changes.
3. Submitted one explicitly consented Standard Ebooks “Suspense” job through the
   running API. Completed download/validation and handoff on attempt 1.
4. Original SHA-256:
   `0a2bd035afd352465c4f1ef4b95064160e1bf8e1a4e49153174254ec3d6975d9`.
5. Read-only Calibre query: matching title count **0 before → 1 after**.
6. CWA logs identify the exact job file
   `gutenberg-6880571270782618-4b5a635702294325b3c848cd7577ca1b.epub` as detected,
   processed by its EPUB fixer, added to its database and successfully processed.
   RoyalBooks itself never wrote the library database.

**The actual CWA import succeeded.** CWA fixed three EPUB issues, changing file
contents. Consequently the application's strict identical-hash presence check
correctly remains `no_exact_match_unverified`; its result is not overwritten with
an invented success. The job's `delivered` state continues to mean handoff only.
The stronger acceptance conclusion above uses the baseline and CWA's exact-job
logs together. The imported test title remains in Calibre.

Backups on NAS: `backups/v2-20261025/source-before.tar.gz`,
`data/backups/v2-20261025/before.sqlite3`, saved ingest ACL and Docker image
`royalbooks:pre-v2-20261025`. Credentials were read directly for authentication,
never printed or included in Git or Docker context. Existing `.env` values were
retained except the two CWA paths connected to the actual mounts.

## Boundaries

- Modern German commercial ebooks cannot be guaranteed. Metadata is never
  represented as an available file. Source listings and language claims remain
  unverified until the relevant file checks occur.
- Only EPUB acquisition is implemented/validated. PDF/AZW3/MOBI are catalog
  format claims, not tested download pipelines.
- Access refusals, unstable HTML, limited publisher feeds and Wikisource export
  failures remain visible. No access restriction was bypassed.
- Full Calibre import completion is not automatically established by the strict
  hash API when CWA rewrites a book; the actual NAS acceptance used CWA logs.
- This PR is not merged automatically. The NAS was updated explicitly at the
  user's request and has rollback backups.

## Changed components

Central registry, new coordinator/query analysis, existing catalog adapters,
common book/evidence model, grouping, tokenless search API, additive job consent
migration, worker deadline, CRC verification, deterministic SQLite closure,
portable storage preflight, responsive UI/detail view, Compose/defaults,
Docker-context exclusions, unit/integration/browser checks and documentation.

Full file list: `git diff --name-only main...feature/royalbooks-2-integrated-providers`.
Visual evidence: [actual NAS desktop](screenshots/nas-desktop.png) and
[actual NAS mobile](screenshots/nas-mobile.png).

## LibGen follow-up

The original link-only EPUB filter and missing LibGen acquisition route were
corrected after the user identified the gap. Full follow-up results and scope:
[free LibGen acquisition](libgen-acquisition.md). The earlier 170-test result
above describes the initial acceptance, not this subsequent change.

## Calibre bookshelf follow-up (2026-10-25)

- The start screen now shows registered Calibre books and their original covers,
  read only from the existing library mount. The deployment observed five books.
- Added authenticated, paginated shelf/search/sort and contained cover/EPUB
  endpoints. No Calibre database or original file is modified.
- Kindle-inspired library navigation, mobile bottom bar, grid/list selection,
  persistent light/dark appearance, book dialog and device EPUB download.
- NAS Linux suite: 205 passed. Windows: 203 passed, two symlink privilege skips.
- Real Chromium tests at 1440, 390 and 320 pixels: shelf, Unicode title/author
  search, sorting APIs, view/theme persistence, modal, file transfer, empty state,
  navigation and no horizontal overflow. Existing discovery/watchlist/queue tests
  remain passing. Both browser scripts are included in CI.
- Live NAS: health OK after container recreation; five library entries, original
  covers and existing EPUB delivery verified. Desktop/mobile library, downloads,
  detail dialog, theme persistence and automatic private-network access verified.
- Previous image retained as `royalbooks:before-bookshelf-20261025`. Existing data
  and all four delivered job records preserved. No active job interrupted.
- EPUB reading uses the device's reading app. Built-in reader, progress sync and
  Kindle delivery are not implemented.

## Storefront and reliability follow-up (2026-10-25)

- Default discovery page: current-year German EPUB editions, recent uploads,
  fiction, nonfiction and actual Open Library daily trends. The live NAS audit
  filled all five shelves with 18 curated records each (shelves may overlap).
- Snapshots persist across restarts, update on startup/hourly in the background,
  retain last-good results on failed/empty source responses, and expose timestamps.
  Loading the store does not run a new search or enqueue downloads.
- Added real LibGen source-listed covers and ISBNs, including parsing the extra
  cover column. A bounded, pinned JPEG proxy supplies the source-required Referer
  and caches covers on the NAS. Live original JPEG bytes verified through HTTP.
- Selected ISBN/work descriptions use bounded, cached Open Library metadata.
  Trend works are never called German editions or available downloads. Source
  text is rendered as text; no generated synopsis or invented bestseller ranks.
- Current-year source filters, filename-metadata curation, horizontal book rails,
  category selection, search sorting and richer details tested at desktop,
  390-pixel mobile and 320-pixel small mobile widths. Existing library, watchlist,
  session and queue browser flows remain passing.
- Search coverage now uses real 25-row paging, upstream language/EPUB filters,
  ISBN-column search, visible known spelling correction and an independent
  Memory of the World source. Live German EPUB candidates: Es 14, Die Therapie
  12, Verity 7, Der Wüstenplanet 29, Harry Potter 16. A separate Memory of the
  World EPUB was downloaded and validated only in temporary container storage.
- A pre-existing authorized failed user job exposed a further actual source CDN:
  the catalog-issued 307 pointed to `cdn5.booksdl.lc/get.php`, with unchanged MD5
  and the expected key shape; its response was HTTP 200. That exact host was
  added, without wildcards. Transfers now retain the catalog Referer. Existing
  wrong-origin, wrong-MD5 and malformed-key rejection tests remain active.
- Windows: 232 passed, two symlink privilege skips. Three offline real Chromium
  scripts cover discovery, bookshelf and store. Live desktop/mobile store,
  original covers, descriptions, corrected multi-author EPUB search and private
  network access verified on the NAS. CI includes all three browser scripts.
- Previous production image retained as `royalbooks:before-store-20261025`; a
  fresh SQLite backup was taken before deployment. Calibre library files and
  metadata are never directly modified by RoyalBooks.

The subsequent user screenshot exposed another catalog-issued HTTPS redirect,
this time `cdn4.booksdl.lc/get.php`, again with the same selected MD5 and expected
key parameters. This exact host is now supported as well. Both original failed
jobs retain their original IDs, recorded user consent and three-attempt cap.
Only each specifically verified failed request was administratively requeued;
unknown redirects remain blocked. The downloads screen now refreshes itself
while visible and presents a readable message for unsupported redirects.
Final local suite: 233 passed, two Windows symlink skips. Final NAS suite:
235 passed. Search browser regressions remain passing after the status update.

## RoyalBooks platform sections and reachable offers (2026-10-25)

- RoyalDownloader's public navigation and settings structure informed the new
  dark/red desktop header, mobile navigation and separate settings console.
  Authors, search, library, discovery, downloads and watchlist have reloadable
  URLs. Settings are split into general, sources, Calibre, system and access.
- Persistent SQLite preferences select sources, language, page size and the
  default reachable-only display. Source changes affect search and storefront.
  Theme stays per browser. No secrets or host paths are returned by settings.
- Author search uses actual source author fields and loads further pages.
  ISBN-10/13 and EAN are checksum validated. Exact bibliographic identification
  can resolve another EPUB edition only with matching title, author and language;
  the interface labels that distinction and retains the EPUB's own identifiers.
- Deduplicated background range probes use three workers, a bounded queue,
  pinned source hosts and same-MD5 redirect validation. A reachable ZIP prefix
  is distinct from full EPUB verification at download. HTML and missing files
  are hidden by default. Probing never creates a download or Calibre import.
- Queue entries retain book display metadata when known, offer status filters,
  refresh while visible and translate common upstream failures. Existing retry
  limits and user consent remain unchanged.
- Thalia's official free catalog is an external link. Its live NAS response was
  HTTP 403; no automated download support or availability is claimed for it.
- Windows suite: 248 passed, two symlink-privilege skips. NAS Linux candidate:
  250 passed. All four real Chromium fixture suites passed; CI now runs the new
  platform suite at desktop, 390-pixel mobile and 320-pixel small mobile widths.
- Live NAS author query: 69 records on page one, 64 on page two; the combined
  display contained 113 grouped works. An EAN lookup resolved three records and
  one EPUB whose source passed the range probe. This is sampled coverage, not a
  claim of complete author bibliographies or permanent file availability.
- Live desktop/mobile browser verification passed author paging, reachable-file
  gating, settings reload, enabled ISBN result, existing seven-book library,
  readable download history, storefront and no horizontal overflow/JS errors.
  No new download job was created by verification. Six delivered jobs and one
  exhausted upstream HTTP-500 failure remain recorded without resetting attempts.
- Deployment retains `royalbooks:before-platform-20261025` plus a fresh SQLite
  backup under `/data/backups/`. Only RoyalBooks was recreated; existing Calibre
  data and persistent LAN access were preserved.


## Cover resolution correction (2026-10-25)

Live source samples exposed 63x100 / 84x100-pixel catalog thumbnails being
stretched over book cards. The same pinned cover paths without `_small` returned
314x500 and 420x500 originals (34-241 KiB). The JPEG proxy now fetches these
originals, upgrades existing cached thumbnails lazily and preserves the original
cache when catalog results register the thumbnail URL again. A versioned browser
URL bypasses the previous one-day thumbnail cache. Open Library covers use its
large variant. Existing origin, redirect, JPEG and 512-KiB limits remain intact.
Twelve artwork checks and the three-width storefront browser suite passed.

## Book-first interface revision (2026-10-25)

Reworked search cards and the shared catalogue/library book dialog around cover,
title and primary actions. Desktop details use a 1080-pixel two-column layout;
mobile uses a compact cover/title header with full-width controls below it.
A single edition selector replaces repeated action blocks. All source records,
ISBNs and technical evidence remain available in a closed disclosure. The
selected edition retains its exact acquisition ID and reachability gating.
Search cards now use larger, uncropped covers and readable type, with an explicit
route from the cover to details. Duplicate technical metadata is removed from
the cards. Artwork remains original-resolution and no description is invented.
Chromium checks cover edition switching, disabled/non-file options, collapsed
ISBN disclosure, dialog width/overflow, keyboard dismissal and existing flows.


## Kiosk start page and shelf coverage (2026-10-25)

The start page no longer hides all catalogue covers until range probes finish.
Browsing defaults to the persisted selection; the optional immediately reachable
filter remains separate from download eligibility. Pending/failed probes never
enable the download action. A browser cache of public catalogue metadata renders
immediately on return/offline visits, with background retries and no stored token.
Shelf snapshots still survive server restarts and upstream errors. Larger source
batches allow up to 36 distinct records per general shelf; four additional author
shelves use actual author-field searches. Counts are based on stored records.

The kiosk has large cover rails, a direct title/author/ISBN search, expanded
all-books shelf views and an optional persistent kiosk layout/fullscreen button.
Leaving browser fullscreen restores the normal layout. Rail positions and focus
survive availability refreshes. The default catalogue stays browsable while file
availability is checked. Browser checks verify cached offline display, pending
file buttons, fullscreen toggling and expanded shelves at 1440/390/320 pixels.
