# RoyalBooks – Book Discovery & Automation

A self-hosted, provider-first ebook discovery tool for Ugreen NAS and other Docker hosts. **v1.0 indexer-first alpha**, not a replacement for Calibre-Web Automated.

## Features
- **Two additional direct-EPUB connectors:** public Wikisource WSIndex finished-book catalog + Wikimedia WS Export (German/English), and Standard Ebooks **public recent-release Atom feed only** (English). Each entry must contain an explicit provider-listed EPUB link; no guessed filenames, no user-controlled URLs, no authentication bypass.
- Direct source identity and publisher URL are bound in SQLite to short-lived, collision-resistant IDs (7-day expiry). Queue workers use the source-specific allowlist for every HTTP redirect, validate the downloaded EPUB, deduplicate SHA-256 and use the existing guarded Calibre handoff.
- **Persistent Merkliste:** catalog books can be saved and removed in the browser, even when no EPUB source is known. This never starts a download; a separate explicit rights confirmation is always needed.
- **In-browser Gutenberg catalog setup:** authenticated one-click CSV and RDF feed imports, readable queued/downloading/indexing/completed/failed status, one import at a time, and restart-safe audit history. No CLI or repeated manual status commands required.
- **Official Project Gutenberg format index:** opt-in import of its *daily RDF bulk archive* containing specific EPUB-format URLs. These are *catalog-listed*, not live verified, and include no clearance for German copyright law. Local Gutenberg search and first-party downloads work independently from the blocked Gutendex API.
- **Official Project Gutenberg offline index:** user-initiated import of the publisher's weekly CSV.gz catalog (~5 MB) into the local SQLite database. No per-search access to blocked Gutendex; no presumed EPUB download links.
- Independent source adapters: **Open Library** (work-level metadata), **Gutendex** (Project Gutenberg catalog), **DNB/German National Library** (bibliographic MARC21 metadata), and optional **Google Books** (metadata only, requires API key).
- Parallel catalog search with per-provider timeout, retry, rate limit and diagnostic success/error counters (SQLite).
- **Provider refusal circuit breaker:** HTTP 403/451 places an origin on a 30-minute cooldown. The Gutenberg worker shares that provider instance and does not claim or exhaust queued download attempts while its catalog origin is blocked. This is intentionally not a bypass for anti-bot controls.
- Conservative catalog cross-references: ISBN-13/title/language edition candidates versus title/author/language work candidates. All original results are preserved; no automatic merging or assumed entitlement to a download.
- **Opt-in read-only provider benchmark** with fixed multilingual cases, explicit language checks, source error counts and measured latency (see below).
- Opt-in Gutenberg EPUB queue: Gutendex entries require a fresh US-public-domain-flag lookup; **first-party RDF entries require a locally indexed official EPUB URL**, with no unsupported copyright presumption. Both routes require explicit user confirmation of rights in their country, host/redirect allowlisting, size limits, EPUB validation and SHA-256 duplicate checks.
- Manual retries only for explicitly classified transient errors, limited to three download attempts **including interrupted attempts during repeated container restarts**; ambiguous handoffs and permanent validation failures cannot be retried automatically.
- Revalidate the staged EPUB against its original SHA-256 digest before the Calibre handoff, and refuse to replace an already existing handoff file.
- Persistent SQLite job statuses: `queued` → `downloading` → `staged` → `delivering` → `delivered`. Errors become `failed`; interrupted handoffs become `needs_review` to prevent accidental repeated imports.
- Optional handoff to the **existing CWA ingest directory**, never writes to `metadata.db`. **`delivered` only means the file was handed off to CWA, not that CWA completed the import.**
- **Optional read-only Calibre presence check:** inspect the actual Calibre `metadata.db` and find an identical EPUB by recorded file size plus SHA-256. Never modifies library content, never treats missing matches as proof of failed import, and never claims the specific RoyalBooks job caused an existing file.
- Read-only handoff diagnostics distinguish files still in the import folder, files no longer there, and inaccessible folders. **A vanished file is not proof of a successful Calibre import.** Ambiguous handoffs remain blocked for manual review.
- API token required for job creation and job listing. No cloud accounts, scraping, torrent downloads, DRM removal or arbitrary URL fetching.

## Direct-source connectors (development preview)

RoyalBooks now includes two more **actual EPUB acquisition pipelines**, not merely metadata searches:

- **Wikisource:** `https://wsindex.toolforge.org/books/` offers a catalog of validated/proofread works and published EPUB export URLs supplied by the Wikimedia WS Export infrastructure. Only the returned `epub_url` on the official exporter domains is eligible. The integration currently scans a *bounded initial catalog page*, so it is **not yet a complete global title search**. Source availability and exporter rate limits must be tested live. The associated German translation or edition may have separate copyright restrictions.
- **Standard Ebooks:** `https://standardebooks.org/feeds/atom/new-releases` is the publisher's publicly accessible, 15-book new-releases feed. RoyalBooks accepts **only explicit `rel=enclosure` EPUB links** from that public feed on the publisher's domain. The complete OPDS/Atom catalogs may require approved membership or project access. RoyalBooks neither evades that access restriction nor guesses hidden download URLs. Existing ebooks remain downloadable on the publisher's website.

Both sources are searchable from the existing RoyalBooks UI and submit one-click EPUB acquisition jobs after the user confirms rights for that title in their country. Entries without trustworthy direct EPUB links are never given a download action. Link listings do not guarantee the server still serves the file. A search populates an expiring local source record; when that record is older than seven days, searching again refreshes it. All download activity remains token-protected; same EPUB validation, 32 MiB cap, safe redirects, error states, hashes and optional CWA import handoff apply.

**No commercial DRM bypass, paid-catalog scraping, credential workarounds or access-control evasion.** Free personal use does not itself grant rights to copy paid ebooks. Library-owned files can still be imported via existing lawful CWA mechanisms. This scope also keeps publisher APIs from being abused.

### Related open-source work reviewed

- [Wikimedia WS Export](https://github.com/wikimedia/ws-export), maintained at [Wikimedia GitLab](https://gitlab.wikimedia.org/toolforge-repos/wsexport), documents the official EPUB export mechanism.
- [Wikisource tools](https://github.com/ruslanbay/wikisource-exporter) demonstrate a different packaging approach; integration instead uses Wikimedia's public export infrastructure.
- [BookShelves OPDS catalog list](https://github.com/getbookshelves/opds-catalog) is a useful discovery list but includes feeds requiring authentication or which may be retired. Any additional OPDS connector must explicitly distinguish free acquisition from borrowing, purchase and preview links.

## NAS host audit: actual CWA / RoyalBooks Docker mounts

This is the **first required step** for the real Ugreen NAS acceptance. Unlike the in-container preflight, the host-side audit reads the real Docker bind sources for BOTH applications. It is deliberately **read-only**, does not need a Docker socket inside RoyalBooks, and does not start containers or touch the existing Calibre database.

From the RoyalBooks checkout on the NAS, run (requires Python 3 and read permission for Docker):

~~~bash
python3 scripts/nas_cwa_audit.py
# Optional machine-readable diagnostics:
python3 scripts/nas_cwa_audit.py --json
# If there are multiple CWA instances:
python3 scripts/nas_cwa_audit.py --cwa-container calibre-web-automated
~~~

The command prints the host directories currently mounted as **CWA** \`/cwa-book-ingest\` and \`/calibre-library\`. Put **those exact host paths** into RoyalBooks's local \`.env\` as \`ROYALBOOKS_CWA_INGEST_PATH\` and \`ROYALBOOKS_CWA_LIBRARY_PATH\`, then start RoyalBooks and run the audit again. **Do not publish .env or share tokens**. The audit does not read env variables, but its output includes host directory names; review them before sharing.

A successful mount comparison is reported as \`mounts_match_not_import_verified\` – **not** as an import success. This only means both containers see the same host ingest directory and RoyalBooks's library mount is read-only. The host directory must also be writable by the effective RoyalBooks UID; after both containers are running use the separate **NAS-Einrichtung prüfen** button in RoyalBooks. Never mount \`/var/run/docker.sock\` into the RoyalBooks container and never change ownership of the CWA library just to make an audit pass.

**Real end-to-end acceptance** requires a deliberately chosen, lawfully obtainable small EPUB, a verified completed download, its safe handoff to CWA, and an exact-library-presence check or manual inspection of CWA. Neither a vanished ingest file nor a passing mock test proves import success. Do not enable full automation until these checks pass on the NAS.

## NAS / CWA integration preflight (v0.7, offline)

**RoyalBooks has NOT been tested against your actual NAS or CWA library.** Before enabling automatic handoff, check the physical host directories configured in **both** Compose stacks. Official CWA expects a dedicated host mount at `/cwa-book-ingest` and a separate library at `/calibre-library`; the ingest folder is emptied after processing. A folder that exists only in the RoyalBooks container does not prove CWA can see it.

After configuring the mounts and starting RoyalBooks, run:

```bash
docker compose exec royalbooks python -m royalbooks.preflight
docker compose exec royalbooks python -m royalbooks.preflight --json
```

You can also open the RoyalBooks UI, enter your API token and click **NAS-Einrichtung prüfen**. It calls the authenticated, read-only `/api/preflight` endpoint and displays errors/warnings in the browser. Neither option proves that CWA watches or imports a handed-off book.

This performs only read-only directory/permission/space checks and a **read-only** SQLite schema query. It never creates an ingest directory, writes a probe book, changes ownership, imports a file or contacts the internet. Failures are reported with exit code 2. Checks for file ownership/write access are heuristic: a successful check does **not** prove that the CWA watcher is consuming that exact host folder.

**Critical safety change:** If the configured ingest directory is missing, a staged EPUB is retained and its job moves to `needs_review` with `ingest_directory_missing`. RoyalBooks no longer silently creates an unwatched directory or claims the book was delivered. Do not retry an ambiguous handoff without inspecting both CWA and RoyalBooks directories. CWA may remove files even when import fails.

The CI suite now exercises a complete **simulated** flow: live queue worker, mocked Gutenberg HTTP, validated EPUB staging, handoff to a fixture ingest folder, mock consumer registration into a fixture Calibre SQLite library, exact SHA-256 presence check, and restart recovery. This is not a real NAS/CWA end-to-end result.

Official CWA reference: https://github.com/crocodilestick/Calibre-Web-Automated/blob/main/docker-compose.yml

## Ugreen NAS installation

1. Copy `.env.example` to `.env`. Set `ROYALBOOKS_BIND_IP` to your **NAS LAN IP**, `ROYALBOOKS_DATA_PATH` to a persistent directory, and generate a long `ROYALBOOKS_API_TOKEN` with `python -c "import secrets; print(secrets.token_urlsafe(32))"`. A blank token disables job creation.
2. DNB bibliographic search is enabled by default; set `ROYALBOOKS_DNB_ENABLED=0` to disable it. Optionally configure `GOOGLE_BOOKS_API_KEY` to enable another metadata source. Queries are sent to Google when enabled. Set `ROYALBOOKS_CWA_INGEST_PATH` **only** if you know the existing host directory mounted in Calibre-Web Automated at `/cwa-book-ingest`. The directories must be *the same host path*. If unsure, leave it blank; downloaded EPUBs remain staged until configured. CWA consumes/removes ingested files.
3. Prepare the bind mounts for the non-root container (UID `10001`), e.g. `mkdir -p data && sudo chown -R 10001:10001 data`. The CWA ingest directory also needs write access for this UID (prefer an ACL/shared group over changing ownership of an existing service directory).
4. Run `docker compose up -d --build`. Open `http://NAS-IP:8094` (or localhost if bind IP has not been changed). Enter the API token into the web UI when enqueueing books. The token is held in page memory only.

Default configuration binds to **127.0.0.1**, so it is not visible on the LAN until explicitly configured. The search UI has no login; **do not expose RoyalBooks publicly, including via tunnels, until full login/access control is implemented**. The API token protects only queue operations, not search. For private remote access, consider an authenticated VPN.

## API
- `GET /api/search?q=Kafka&language=de`: mixed catalog listings; metadata results ≠ downloads.
- `GET /api/providers`: anonymized source quality counters (the last 200 attempts; no search terms stored).
- `POST /api/jobs`: `X-API-Key: <token>`, JSON `{"provider":"gutendex","source_id":42,"rights_confirmed":true}`.
- `GET /api/jobs`: API key required, last 100 jobs, ingest/library configuration, and `handoff_observation` for each staged/delivered/ambiguous job.
- `GET /api/jobs/{id}/library-check`: API key required, optional read-only exact EPUB hash check against Calibre `metadata.db`; does not imply causal import confirmation.
- `POST /api/jobs/{id}/retry`: API key and `{ "rights_confirmed": true }` required; allowed only for transient failed jobs with fewer than three attempts, otherwise HTTP 409.
- `GET /api/health`: health/version.
- `GET /api/preflight`: API key required; read-only NAS/CWA configuration diagnostics without displaying the token or host paths. The web UI exposes these checks via **NAS-Einrichtung prüfen**.
- `GET /api/watchlist`, `POST /api/watchlist`, `DELETE /api/watchlist/{id}`: authenticated local reading list. User-entered source IDs and external HTTPS metadata links are validated. Saving is *not* an acquisition request.
- `GET /api/catalog/imports`: authenticated history of the 20 latest official-feed imports.
- `POST /api/catalog/imports`: authenticated, explicit import of either `{"source":"csv"}` or `{"source":"rdf"}`. Only one active import; second request returns HTTP 409. Archive requests and local parsing have bounded sizes, use existing transactional importers and touch no Calibre files.

### Development

```bash
python -m pip install -e '.[test]'
python -m pytest -q
ROYALBOOKS_DB_PATH=./data/dev.sqlite3 uvicorn royalbooks.app:app --reload --port 8094
```

Tests use offline/mock downloads and simulated CWA ingest. Actual network availability, German copyright status and real NAS/CWA import behavior **have not yet been verified**. A queued retry still checks the rights flag again and re-fetches metadata and EPUB; immutable source IDs cannot be retried if the failure was invalid, duplicate, forbidden or ambiguous. **Never interpret `delivered` as proof that CWA indexed a book.**

## Reliable offline Gutenberg search (first-party bulk catalog)

A recent live audit found the third-party Gutendex API blocked some clients. The official Project Gutenberg site provides a weekly CSV catalog and explicitly recommends its **bulk feeds instead of crawling**. RoyalBooks can index that file in your NAS's local SQLite database without touching the Calibre library. This solves the **catalog search dependency**, but not the different problem of verifying whether an EPUB exists for any given catalog entry.

The index is deliberately initiated by the user: **no automatic periodic downloads** and no provider bypass. The browser's **Offiziellen Gutenberg-Katalog einrichten** panel offers buttons for both official feeds and displays their import progress as named stages. Large RDF archives may require substantial disk space and several minutes of processing; they are imported into a transactionally replaced local SQLite index. The API token is required for both start and status requests. The following CLI commands remain available as a fallback: With the NAS Docker service running and the data directory writable by UID 10001:

```bash
# One explicit, bounded request for the current official compressed CSV catalog:
docker compose exec royalbooks python -m royalbooks.gutenberg_catalog --download

# Alternatively, use an already downloaded official CSV or CSV.gz:
docker compose exec royalbooks python -m royalbooks.gutenberg_catalog --file /data/pg_catalog.csv.gz
```

The updater limits the compressed transfer to 12 MiB, validates CSV headers and row counts and swaps the local index **transactionally** (the previous index remains intact if parsing fails). After import, the search provider `gutenberg_catalog` is available in the ordinary book search and the API reports `catalog.indexed`, row count and update timestamp at `GET /api/providers`.

**Important:** Gutenberg's CSV does **not contain** file-format availability, verified EPUB links, or rights in Germany. These records appear strictly as `catalog_only_epub_unverified`. They are never enqueued for download by this adapter. Existing Gutendex download support remains opt-in and separate. The CSV's `Issued` date is a Gutenberg release date, **not the original print publication year**.

Official sources:
- [Project Gutenberg bulk catalog and usage advice](https://www.gutenberg.org/ebooks/offline_catalogs.html)
- [Official feed directory](https://www.gutenberg.org/cache/epub/feeds/)

## Official EPUB format metadata – second offline index

Project Gutenberg publishes the daily `rdf-files.tar.bz2` archive in addition to the smaller weekly CSV. The RDF includes EPUB MIME types and original format links, so RoyalBooks does not need to guess per-book download URLs or query the third-party Gutendex API.

**Browser:** Enter the API token, click **1 · Buchkatalog laden**, then **2 · EPUB-Formate laden**. Each source import happens only after an explicit click and will be retried only after an explicit new request. These optional CLI commands are for recovery or automation by the NAS administrator:

```bash
# Step 1: Index the titles (compressed CSV ~5 MB)
docker compose exec royalbooks python -m royalbooks.gutenberg_catalog --download
# Step 2: Index official EPUB format metadata (compressed RDF >100 MB)
docker compose exec royalbooks python -m royalbooks.gutenberg_formats --download
# Offline alternative if the official tarball is already in the RoyalBooks /data volume:
docker compose exec royalbooks python -m royalbooks.gutenberg_formats --file /data/rdf-files.tar.bz2
```

The archive is read in streaming mode without extracting its files to disk. An import interrupted by a container restart is shown as **interrupted** and cannot be assumed complete; an existing successfully built index remains in place until a later successful replacement. Import is transactional: malformed archives do not destroy the existing index. The metadata transfer is capped at 256 MiB compressed, with per-record and decompression limits. It is intentionally not automatic in the preview.

After both imports, the local search labels indexed files as **"official RDF EPUB link, not live verified"**. If you have independently checked your rights to a particular work, select it and enter the normal API token. RoyalBooks downloads only the allowlisted first-party URL, then validates its actual ZIP/EPUB structure and digest before handing it to the CWA ingest folder. **A metadata link alone never proves actual availability, successful download, or an import into Calibre.** Some redirects may be refused for safety. File sizes over the current 32 MiB download limit are rejected.

The new download source ID is `gutenberg_catalog` (the original `gutendex` remains supported independently). The persistent queue and previous database schema remain compatible.

Sources: [official Gutenberg feeds](https://www.gutenberg.org/cache/epub/feeds/) and [bulk metadata guidance](https://www.gutenberg.org/ebooks/offline_catalogs.html).

## Optional Calibre library verification (read-only)

For this optional check, set `ROYALBOOKS_CWA_LIBRARY_PATH` in `.env` to the **host directory** that your existing CWA container mounts at `/calibre-library`. This must be the library directory containing `metadata.db`, **not** the ingest folder. RoyalBooks mounts it at `/cwa-library:ro` (strictly read-only); the original CWA library must continue to be managed only by CWA/Calibre. Do not change ownership or database permissions solely for RoyalBooks.

The authenticated `GET /api/jobs/{id}/library-check` endpoint is available for jobs marked `delivered`. The button **In Calibre prüfen** performs this one check on demand. It reads registered EPUB format entries from Calibre's standard `books` / `data` tables and computes SHA-256 only for bounded candidate files of the same recorded size (100 max). It follows no symlink outside the mounted library and does not crawl the directory tree. If the exact hash exists, the answer is `identical_epub_present`: an identical EPUB is **present in the library**. This is **not proof that this job imported the book**; the file may have existed beforehand.

If no exact match exists, the result is `no_exact_match_unverified` rather than `failed`. CWA can rewrite EPUBs, calibre's size metadata can become stale, and filesystem/SQLite state can lag. An ingest file disappearing still does not prove success. This feature doesn't require write access, an undocumented CWA API or scraping. If SQLite cannot read a live WAL-enabled library on a read-only mount, the result stays inconclusive, rather than changing CWA settings.

**Real NAS validation is still required**: no access to your current library was performed in CI, only fixtures. The existing Calibre import handoff and job states remain unchanged.

## Source benchmark (manual, read-only)

The included 12-case corpus is in `benchmarks/cases.json` and deliberately mixes German classics, English books and contemporary protected bestsellers. It measures **catalog metadata search quality only**, never unauthorized downloads:

```bash
# Examine selected providers and queries without network traffic:
python -m royalbooks.benchmark
# Explicitly run public catalog queries, then inspect results:
python -m royalbooks.benchmark --live --output data/provider-benchmark.json
# Optional, scoped to a specific provider:
python -m royalbooks.benchmark --live --providers dnb --limit 10
```

The JSON report differentiates successful HTTP queries, matching titles with **known language**, and unverified EPUB catalog links. Match rates are not download success rates. No benchmark has been run against your NAS yet; the default mode does not issue network requests. DNB and Open Library need conservative traffic rates and their APIs must not be treated as bulk-crawling services.

## One-time GitHub source audit

The separate `Provider source audit` workflow runs once when its workflow file or the benchmark definitions change on the development branch. It queries the 12 fixed test books against Gutendex, Open Library and DNB, with no downloads, then uploads `royalbooks-source-audit` JSON as a temporary GitHub Actions artifact. It does **not** assert a provider is healthy solely because its API returned records; inspect the language-aware match rate and error counts. Live search data may differ by region, time, and catalog limits. Provider-specific failure codes (such as HTTP 403) are preserved in the report instead of being replaced by a generic Python exception name. This is a catalog-metadata audit, **not** an EPUB availability or copyright test.

## Critical provider finding (October 2026)

Our initial 12-case GitHub live benchmark returned **0 successful catalog responses from Gutendex**, while Open Library and DNB responded to all 12 queries. External catalog-health checks also report an HTTP-403 protection challenge at gutendex.com. RoyalBooks will respect that denial (no scraping/captcha/proxy evasion), keep other sources working, and avoid consuming Gutenberg queue attempts during the cooldown. A separate, first-party Gutenberg bulk catalog or locally hosted search index is preferable to treating a protected community API as dependable infrastructure. **This finding does not mean Gutenberg itself is down.** The GitHub report is metadata-only and proves no successful ebook downloads.

## Sources and legal limitations
The DNB and Google Books adapters are metadata-only: a preview or search result is **not** a legal download offer. Project Gutenberg's Gutendex catalog has an optional `copyright` field that concerns **US copyright status**; this alone does not prove that a German user can download the title. RoyalBooks therefore requires explicit local-rights confirmation for every acquisition. Open Library provides work-level records only and never becomes a download provider in this stage. **Work-level ISBN lists are not reliable edition identifiers and are intentionally not used as exact ISBN matches.** Useful upstream documentation:
- [Gutendex API](https://github.com/garethbjohnson/gutendex)
- [Open Library API](https://openlibrary.org/developers/api)
- [DNB SRU API](https://www.dnb.de/DE/Professionell/Metadatendienste/Datenbezug/SRU/sru_node.html)
- [CWA Docker ingest volume](https://github.com/crocodilestick/Calibre-Web-Automated/blob/main/docker-compose.yml)

## Next milestone
Validate real first-party EPUB transfer reliability and NAS/CWA import completion without bypassing provider restrictions; improve source matching and confirm live benchmark results, improve edition-level mappings using authoritative edition records, add explicit safe recovery for ambiguous handoffs and independently verify Calibre import status. Authenticate the whole UI before any public exposure.

## NAS startup troubleshooting (non-root permissions)

RoyalBooks deliberately runs as Linux UID 10001 rather than root. Some NAS-based repository checkouts preserve `0600` Python files or non-traversable directories in the Docker build context. Older images could then restart repeatedly with `PermissionError: [Errno 13] Permission denied: '/app/royalbooks/__init__.py'` even though Python and Docker builds passed in CI.

The Dockerfile now explicitly normalizes **only packaged source-code permissions** inside the image (directories `0755`, files `0644`, owned by root). CI additionally runs the actual built image as UID 10001 and imports the FastAPI application. No changes are made to NAS host file permissions, Calibre data, or the `/data` bind mount.

After updating the checkout, **rebuild** the image; restarting the old container without rebuilding will not fix it:

```bash
git pull --ff-only
docker compose build --no-cache royalbooks
docker compose up -d --force-recreate royalbooks
docker compose logs --tail=80 royalbooks
```

If the next error concerns `/data/royalbooks.sqlite3`, that is a **different host volume ownership/mount permissions issue**. Check it without broad recursive chmod/chown commands against your NAS or Calibre library. If your NAS Compose stack overrides `/app` or `/app/royalbooks` with a bind mount, fix that external mount configuration; the Dockerfile cannot change its runtime permissions.

## Search result presentation (v0.9 UI improvement)

The search API keeps every source-specific record in `items`. `display_groups` is an additional reversible, conservatively keyed presentation index: entries with exactly matching normalized title/language and compatible named authors are rendered on a single card with an expandable list of source records, ISBNs and per-edition actions. This is not edition deduplication or proof of identical files. Different author names and unknown languages are kept separate. Exact title query matches rank first, and a valid EPUB listing is favored for the card's lead record. The browser also separates catalog-only results from listed (unverified) EPUB links and shows provider outages without overwhelming the search page. DNB covers may be requested from Open Library by that record's ISBN, with a visible fallback if unavailable. A Gutendex request is capped at six seconds to avoid delaying all other search providers on blocked hosts.

## Provider capability contracts and German coverage

The GET /api/providers response now contains source_profiles classifying **bibliography**, **EPUB catalogs** and **publisher-listed EPUB sources**. GET /api/search additionally includes search_evidence with an explicit split between metadata-only results and listed-but-unverified EPUB URLs. No bibliographic result is represented as an acquired file. Open Library work-level language is treated as ambiguous where several editions exist. The changes are backward compatible: existing items, display_groups, acquisition queue, CWA mounts and credentials remain unchanged.

The source research shows that DNB is a strong catalog for identifying German editions and ISBNs, while modern projects such as Bindery, Shelfarr and Librarr use indexers or external services separately from metadata. Actual German file coverage must be measured for each configured source. Upstream comparisons: https://github.com/vavallee/bindery ; https://github.com/Pedro-Revez-Silva/shelfarr ; https://github.com/JeremiahM37/librarr ; https://github.com/selmant/shelfbridge . No external archive or indexer credentials are required or collected by this patch.

## Indexer-first RoyalBooks (v1.0 alpha)

The default search uses ONLY operator-configured Prowlarr, Torznab or Newznab connections.
Old Open Library, DNB, Google Books, Gutenberg, Wikisource and Standard Ebooks adapters remain in the repository for historic compatibility but are no longer active search providers.

### Configure Prowlarr on your NAS

Set the following variables in your existing .env file:

    ROYALBOOKS_PROWLARR_URL=http://prowlarr:9696
    ROYALBOOKS_PROWLARR_API_KEY=REPLACE_WITH_YOUR_API_KEY

The hostname prowlarr resolves only if your Docker network connects both containers.
Otherwise use a reachable NAS LAN IP or an HTTPS address. Nothing is installed automatically.
RoyalBooks keeps the credentials on the server; the browser authenticates to RoyalBooks
with ROYALBOOKS_API_TOKEN, entered in the top search form.

### Add Torznab or Newznab endpoints

Copy docs/indexers.example.json to the persistent data/indexers.json and edit only
the values for your own indexers. Configure URLs through the JSON file, not in browser
parameters. The running container must be able to read /data/indexers.json.
Both protocols request category 7020 (Books/EBook). Up to eight entries are supported.
HTTP is accepted only for private/loopback addresses and simple Docker hostnames;
use HTTPS for remote indexer services.

### Search quality and safety

RoyalBooks queries configured indexers in parallel and scores result names by
title-word overlap, advertised EPUB/AZW3/PDF markers, and German/English labels.
Language and format are *unverified filename hints*. Unmarked language stays unknown.
Only sanitized title, indexer, protocol, filesize and score are sent to the browser.
No NZB, magnet or download URLs are returned or automatically imported.

### Migration without data loss

The old SQLite database, watchlist and job history are preserved. Legacy Gutenberg
background downloads and new public catalog imports are disabled by default.
Explicit ROYALBOOKS_ENABLE_LEGACY_WORKER=1 restores old processing if necessary
for independently authorized legacy material. Without it, legacy write endpoints
return HTTP 410. The existing Calibre-Web Automated bind mounts are unchanged.
The indexer feature currently provides read-only search, not automatic downloads.

### Relevant upstream implementations

- Bindery: https://github.com/vavallee/bindery
- Shelfarr: https://github.com/Pedro-Revez-Silva/shelfarr
- Librarr: https://github.com/JeremiahM37/librarr
- ShelfBridge: https://github.com/selmant/shelfbridge

These projects separate metadata/catalog search from externally configured indexers.
Their support for German files depends on live provider availability and is not guaranteed.
Third-party archives with fluctuating mirrors are not installed as fixed source adapters.

## Token-free discovery (v1.0.1)

Normal GET /api/indexers/search requests no longer require entering the RoyalBooks API token. The server retains private Prowlarr/Torznab/Newznab credentials and only exposes sanitized read-only indexer listing data. A basic per-client quota limits search traffic to 30 queries per minute, suitable for a LAN installation. Do not publish the service to the open Internet without an authenticating reverse proxy. Personal watchlist, queue history, NAS diagnostics and all management/write endpoints still require the existing RoyalBooks API token, now located in an optional collapsed admin panel rather than the main search form.
