# RoyalBooks – Book Discovery & Automation

A self-hosted, provider-first ebook discovery tool for Ugreen NAS and other Docker hosts. **v0.5 development preview**, not a replacement for Calibre-Web Automated.

## Features
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
- Read-only handoff diagnostics distinguish files still in the import folder, files no longer there, and inaccessible folders. **A vanished file is not proof of a successful Calibre import.** Ambiguous handoffs remain blocked for manual review.
- API token required for job creation and job listing. No cloud accounts, scraping, torrent downloads, DRM removal or arbitrary URL fetching.

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
- `GET /api/jobs`: API key required, last 100 jobs, ingest configuration, and `handoff_observation` for each staged/delivered/ambiguous job.
- `POST /api/jobs/{id}/retry`: API key and `{ "rights_confirmed": true }` required; allowed only for transient failed jobs with fewer than three attempts, otherwise HTTP 409.
- `GET /api/health`: health/version.

### Development

```bash
python -m pip install -e '.[test]'
python -m pytest -q
ROYALBOOKS_DB_PATH=./data/dev.sqlite3 uvicorn royalbooks.app:app --reload --port 8094
```

Tests use offline/mock downloads and simulated CWA ingest. Actual network availability, German copyright status and real NAS/CWA import behavior **have not yet been verified**. A queued retry still checks the rights flag again and re-fetches metadata and EPUB; immutable source IDs cannot be retried if the failure was invalid, duplicate, forbidden or ambiguous. **Never interpret `delivered` as proof that CWA indexed a book.**

## Reliable offline Gutenberg search (first-party bulk catalog)

A recent live audit found the third-party Gutendex API blocked some clients. The official Project Gutenberg site provides a weekly CSV catalog and explicitly recommends its **bulk feeds instead of crawling**. RoyalBooks can index that file in your NAS's local SQLite database without touching the Calibre library. This solves the **catalog search dependency**, but not the different problem of verifying whether an EPUB exists for any given catalog entry.

The index is deliberately manual for now: **no background downloads** and no automatic provider bypass. With the NAS Docker service running and the data directory writable by UID 10001:

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

**Manual one-time commands**, after the Docker container is running:

```bash
# Step 1: Index the titles (compressed CSV ~5 MB)
docker compose exec royalbooks python -m royalbooks.gutenberg_catalog --download
# Step 2: Index official EPUB format metadata (compressed RDF >100 MB)
docker compose exec royalbooks python -m royalbooks.gutenberg_formats --download
# Offline alternative if the official tarball is already in the RoyalBooks /data volume:
docker compose exec royalbooks python -m royalbooks.gutenberg_formats --file /data/rdf-files.tar.bz2
```

The archive is read in streaming mode without extracting its files to disk. Import is transactional: malformed archives do not destroy the existing index. The metadata transfer is capped at 256 MiB compressed, with per-record and decompression limits. It is intentionally not automatic in the preview.

After both imports, the local search labels indexed files as **"official RDF EPUB link, not live verified"**. If you have independently checked your rights to a particular work, select it and enter the normal API token. RoyalBooks downloads only the allowlisted first-party URL, then validates its actual ZIP/EPUB structure and digest before handing it to the CWA ingest folder. **A metadata link alone never proves actual availability, successful download, or an import into Calibre.** Some redirects may be refused for safety. File sizes over the current 32 MiB download limit are rejected.

The new download source ID is `gutenberg_catalog` (the original `gutendex` remains supported independently). The persistent queue and previous database schema remain compatible.

Sources: [official Gutenberg feeds](https://www.gutenberg.org/cache/epub/feeds/) and [bulk metadata guidance](https://www.gutenberg.org/ebooks/offline_catalogs.html).

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
