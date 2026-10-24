# RoyalBooks

A provider-first, self-hosted book discovery service. **Initial development stage (v0.1)**, not a complete ebook downloader. RoyalBooks currently searches catalog metadata and displays potential public-domain US EPUB links, with no automatic acquisition. It does not interact with an existing Calibre database.

## Features
- Independent asynchronous providers: **Open Library** (metadata only) and **Gutendex** (Project Gutenberg catalog).
- Per-provider timeouts, transient HTTP retries, source-specific rate limits and fault isolation.
- Persistent SQLite provider diagnostics: search attempts, found/empty/error, result count and latency. The 200 latest attempts are summarized; search terms are not stored.
- Responsive first-stage Provider Lab. JSON API: `/api/search?q=Kafka&language=de`, `/api/providers`, `/api/health`.
- Basic input validation; no downloads, scraping or access credentials in this stage.

## Run on Ugreen NAS (Docker)

```bash
cp .env.example .env
# Edit ROYALBOOKS_DATA_PATH to /volume1/docker/royalbooks/data if desired
mkdir -p data
# For the read-only, non-root container ensure bind directory is writable by UID 10001:
# sudo chown -R 10001:10001 data
docker compose up -d --build
```

Open `http://NAS-IP:8094`. By default SQLite lives in `./data`. If using an absolute NAS path, create that directory and give container UID `10001` access. **No login/authentication yet**: keep RoyalBooks on your trusted LAN; do not expose port 8094 publicly. Existing Calibre-Web Automated on port 8083 is untouched.

## Local development

```bash
python -m venv .venv
# activate your venv
pip install -e '.[test]'
ROYALBOOKS_DB_PATH=./data/dev.sqlite3 uvicorn royalbooks.app:app --reload --port 8094
pytest -q
```

## Source and rights boundaries
- `OpenLibrary` yields **metadata only**; its books and borrow/preview badges do not imply a freely downloadable EPUB.
- `Gutendex` reports the Gutenberg *US* copyright flag; any potential EPUB link must be checked against local copyright law. RoyalBooks does not download it automatically.
- Respect provider API rate limits (Open Library default anonymous clients: 1 req/s). For heavier use, register your use case with Open Library first.
- Successful API search ≠ actual downloaded/validated ebook. Source health metrics describe *search behavior only*.

## Next milestones
1. Repeatable source-benchmark suite using licensed/known-public-domain test cases, language accuracy and fixture-based reliability metrics.
2. Provider contracts, deduplication by identifiers, canonical work/edition distinction, tracked retries and throttling.
3. Explicitly authorized download/import queue with file-type validation, duplicate detection and CWA ingest, without writing directly to `metadata.db`.
4. Authentication, multiuser permissions, backups and external access hardening before production exposure.

Upstream API references: [Open Library search](https://openlibrary.org/dev/docs/api/search), [Open Library usage](https://openlibrary.org/developers/api), [Gutendex](https://github.com/garethbjohnson/gutendex).
