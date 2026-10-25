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
