# RoyalBooks – Book Discovery & Automation

A self-hosted, provider-first ebook discovery tool for Ugreen NAS and other Docker hosts. **v0.2 development preview**, not a replacement for Calibre-Web Automated.

## Features
- Independent source adapters: **Open Library** (metadata only) and **Gutendex** (Project Gutenberg catalog).
- Parallel catalog search with per-provider timeout, retry, rate limit and diagnostic success/error counters (SQLite).
- Opt-in Gutenberg EPUB queue: a fresh catalog lookup, US public-domain flag check **plus explicit user confirmation of local rights**, host/redirect allowlisting, stream-size limits, archive validation and SHA-256 duplicate checks.
- Persistent SQLite job statuses: `queued` → `downloading` → `staged` → `delivering` → `delivered`. Errors become `failed`; interrupted handoffs become `needs_review` to prevent accidental repeated imports.
- Optional handoff to the **existing CWA ingest directory**, never writes to `metadata.db`. **`delivered` only means the file was handed off to CWA, not that CWA completed the import.**
- API token required for job creation and job listing. No cloud accounts, scraping, torrent downloads, DRM removal or arbitrary URL fetching.

## Ugreen NAS installation

1. Copy `.env.example` to `.env`. Set `ROYALBOOKS_BIND_IP` to your **NAS LAN IP**, `ROYALBOOKS_DATA_PATH` to a persistent directory, and generate a long `ROYALBOOKS_API_TOKEN` with `python -c "import secrets; print(secrets.token_urlsafe(32))"`. A blank token disables job creation.
2. Set `ROYALBOOKS_CWA_INGEST_PATH` **only** if you know the existing host directory mounted in Calibre-Web Automated at `/cwa-book-ingest`. The directories must be *the same host path*. If unsure, leave it blank; downloaded EPUBs remain staged until configured. CWA consumes/removes ingested files.
3. Prepare the bind mounts for the non-root container (UID `10001`), e.g. `mkdir -p data && sudo chown -R 10001:10001 data`. The CWA ingest directory also needs write access for this UID (prefer an ACL/shared group over changing ownership of an existing service directory).
4. Run `docker compose up -d --build`. Open `http://NAS-IP:8094` (or localhost if bind IP has not been changed). Enter the API token into the web UI when enqueueing books. The token is held in page memory only.

Default configuration binds to **127.0.0.1**, so it is not visible on the LAN until explicitly configured. The search UI has no login; **do not expose RoyalBooks publicly, including via tunnels, until full login/access control is implemented**. The API token protects only queue operations, not search. For private remote access, consider an authenticated VPN.

## API
- `GET /api/search?q=Kafka&language=de`: mixed catalog listings; metadata results ≠ downloads.
- `GET /api/providers`: anonymized source quality counters (the last 200 attempts; no search terms stored).
- `POST /api/jobs`: `X-API-Key: <token>`, JSON `{"provider":"gutendex","source_id":42,"rights_confirmed":true}`.
- `GET /api/jobs`: API key required, last 100 jobs and staging/import configuration.
- `GET /api/health`: health/version.

### Development

```bash
python -m pip install -e '.[test]'
python -m pytest -q
ROYALBOOKS_DB_PATH=./data/dev.sqlite3 uvicorn royalbooks.app:app --reload --port 8094
```

Tests use offline/mock downloads and simulated CWA ingest. Actual network availability, German copyright status and real NAS/CWA import behavior **have not yet been verified**.

## Sources and legal limitations
Project Gutenberg's Gutendex catalog has an optional `copyright` field that concerns **US copyright status**; this alone does not prove that a German user can download the title. RoyalBooks therefore requires explicit local-rights confirmation for every acquisition. Open Library provides book records only and never becomes a download provider in this stage. Useful upstream documentation:
- [Gutendex API](https://github.com/garethbjohnson/gutendex)
- [Open Library API](https://openlibrary.org/developers/api)
- [CWA Docker ingest volume](https://github.com/crocodilestick/Calibre-Web-Automated/blob/main/docker-compose.yml)

## Next milestone
More metadata sources, matching by ISBN and edition, provider reliability benchmarks, job retry controls and import confirmation. Authenticate the whole UI before any public exposure.
