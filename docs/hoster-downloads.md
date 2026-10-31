# Byte.to and free-hoster downloads

RoyalBooks separates catalog discovery, link-container resolution, hoster transfer,
EPUB validation and Calibre handoff. A Byte.to release or a downloader package
marked finished is never treated as proof of a completed book download.

## Using the new source

Open `/hosters` (also linked from Downloads and Settings → Sources). Browse latest
Byte.to entries or search by title/author. The normal provider registry includes
Byte.to; existing saved source selections remain unchanged until edited.
The dedicated source page always makes Byte.to accessible. Catalog rows have no
verified file language or format; release-title author splitting is only a hint.

Only ebook detail routes are parsed. Category pagination retains all 200 upstream
rows while displaying 25 at a time. Pages are cached for five minutes. Search
results from other Byte.to categories are excluded; a global source search can
contain fewer ebook matches than the site-wide result count. No full 46,000-title
crawl runs at startup.

A confirmed source-indexed ID enters a separate persistent hoster queue. The
resolver follows only source-listed widget buttons and their published redirects
to approved Filecrypt/Nitroflare/ddownload routes. It ignores affiliate banners,
invisible Cloudflare links and unrelated iframes. Access refusals are not bypassed.

JDownloader receives one atomic, idempotent `.crawljob` per request, retaining
its package/download state independently of RoyalBooks restarts. It handles the
hoster's normal free-download flow, waits, retries and supported extraction.
Resolution transport errors retry after 60/300 seconds, at most three attempts.
There is no promise that free hosters or link protectors will allow automation.

The status sidecar shares only JDownloader's network namespace and calls its
localhost-only API. Port 3128 is never published. It exports only RoyalBooks job
IDs, coarse states and byte counts; no file URLs, credentials or unrelated user
packages. Cloudflare failures become an explicit stop. A package can be marked
finished by JDownloader even when all links failed: this is not accepted as success.

After normal browser verification, the user can submit Nitroflare/ddownload file
links for the same blocked job. Filecrypt container links cannot be mistaken for
those direct hoster file pages. Resume refuses delivered/ambiguous handoff jobs and
duplicate clicks. Human-verification challenges are not bypassed or auto-solved.

Files stay in per-job directories. Partial files, invalid EPUBs, changed files,
symlinks, multiple different EPUBs and insufficient title matches are not silently
imported. Complete EPUB ZIP/CRC/package checks, a settling period, title matching,
duplicate checks and the existing atomic Calibre ingest handoff are required.
Uncertain handoffs require review. Archive collections need manual selection;
RoyalBooks does not silently bulk-import them.

## Optional Docker setup

Use `compose.yaml` together with `compose.hosters.yaml` and any existing operator
overrides. The downloader is independent of Calibre and has no Calibre library
mount. No premium, disposable-email or MyJDownloader account is created.

1. Set a separate `ROYALBOOKS_DOWNLOADER_PASSWORD` in the private `.env`, the LAN
   bind IP and `ROYALBOOKS_HOSTER_CONTROL_URL=https://NAS:8095`. Do not reuse SSH
   credentials. Prefer a pinned `ROYALBOOKS_DOWNLOADER_IMAGE` digest.
2. Prepare writable directories under the data root: `hoster/watch`,
   `hoster/received`, `hoster/engine-state`, `hoster/jdownloader`, UID/GID 10001.
3. Let the downloader initialize its default configuration before adding custom
   cfg files. Creating only a partial cfg directory breaks the image initializer.
   If preconfiguring, copy the image's `/defaults/cfg` completely first.
4. Install and enable Folder Watch through the downloader's supported extension
   management. Set its active Folders setting to `["/watch"]`, then verify the
   setting through the application. Config-on-disk alone did not establish the
   active setting during this audit. Updates may require a downloader restart.
5. Enable the local RemoteAPI settings `DeprecatedApiEnabled=true` and
   `DeprecatedApiLocalhostOnly=true` for the observer. Keep port 3128 unpublished.
   Start `book-downloader-status` with the current RoyalBooks image; if using a
   different Compose project image name, set `ROYALBOOKS_ENGINE_STATUS_IMAGE`.
6. The GUI is HTTPS with separate authentication on port 8095. The generated local
   certificate may require normal browser trust confirmation. Hoster accounts
   remain optional; free downloads may need browser interaction.

Only RoyalBooks-owned folders and services were added in the NAS deployment.
The original application image, `.env` and SQLite database were backed up. Existing
queued transfers were checked before application restart. The downloader image
was pinned to the pulled digest; no paid service was provisioned.

## Live evidence on 2026-10-31

- Byte.to category 43 returned HTTP 200 and advertised 46,785 entries, with 200
  ebook rows on page one. This is a source count, not a verified download count.
- The sampled detail page returned two source widgets; their buttons redirected
  normally to Filecrypt containers. Affiliate links were excluded.
- The actual Byte.to job reached JDownloader, which reported `Blocked by!Cloudflare
  Site-Protection` for both containers. No EPUB was obtained from those containers.
- The older official JDownloader speed.zip example was offline. A replacement
  first-party Gutenberg EPUB test completed through the same downloader: 191,405
  bytes, SHA-256 `6e0ca94e1a76c247983f5e680c17f15763ace9dfade35714aa1b58e1788877ea`.
  RoyalBooks full EPUB validation passed. This diagnostic file was not imported
  into the user's Calibre library.
- Windows: 298 tests passed, two existing symlink permission skips. New tests cover
  source parsing/pagination, URL boundaries, queue consent, bounded retry,
  duplicate dispatch, title/partial-file protection, engine error classification,
  stale observations and manual recovery. Chromium covers source browsing,
  first-load authentication, Cloudflare status and manual-link requests at
  desktop/390/320 widths. See test-report.md for final deployment checks.

## Primary references

- Byte.to category: https://byte.to/?cat=43
- Folder Watch: https://support.jdownloader.org/en/knowledgebase/article/folder-watch-basic-usage
- API methods: https://my.jdownloader.org/developers/
- Source: https://github.com/mirror/jdownloader
- Container: https://github.com/jlesage/docker-jdownloader-2
- ddownload published API: https://ddownload.com/api (the displayed v2 endpoints
  are largely account/file management; they are not assumed to provide anonymous
  file download links).


## GitHub research on the human-verification blocker

Reviewed the current primary repositories of FlareSolverr, Byparr and JDownloader
on 2026-10-31. FlareSolverr uses a browser for challenge handling and supports
persistent sessions; its README still explicitly warns that the configured
CAPTCHA solvers do not work. Its documentation also exposes Turnstile-related
options, so browser challenges must not be conflated with all CAPTCHA types.
Byparr explicitly does not guarantee challenge completion. Neither repository
was tested as a working bypass of this NAS/Filecrypt container, and neither was
installed or enabled as one. JDownloader's FileCryptCc plugin contains normal
container/CNL and captcha handling; the observed Cloudflare refusal occurs before
an actual hoster transfer. Source claims and older reports of successful forks
are not accepted as evidence that this particular protected container works.

References:
- https://github.com/FlareSolverr/FlareSolverr/blob/master/README.md
- https://github.com/ThePhaseless/Byparr/blob/main/README.md
- https://github.com/mirror/jdownloader/blob/master/src/jd/plugins/decrypter/FileCryptCc.java


## Solverr installation and live test (2026-10-31)

The optional `compose.solverr.yaml` now starts Solverr 1.9.1 independently of
RoyalBooks, JDownloader and Calibre. The downloaded image is pinned by digest;
its OCI revision matches `36294f132b7020d2210be1a8965df9658023b82c`. There are no
published ports or NAS bind mounts. The process is non-root, limited to 2 GiB RAM,
2 CPUs and 512 PIDs. Both free browser engines are enabled; paid providers and
API keys are explicitly disabled. Only the new service was started.

Use the normal Compose files plus `-f compose.solverr.yaml` and start only
`book-solverr` with `up -d --no-deps book-solverr`. Containers in the same Compose
network reach it at `http://book-solverr:8191`. To stop this optional component,
use the same Compose files with `stop book-solverr`; existing downloads remain
independent. Keep port 8191 unpublished because the service has no authentication.

Live evidence:
- `/health` and `/` returned HTTP 200; the API reported version 1.9.1.
- The user's exact Filecrypt container was tested with a shared 120-second
  budget. Chrome timed out after about 60 seconds. Solverr automatically fell
  back to Camoufox, which ended with "Challenge still present after solving
  attempts". The service returned HTTP 500. The first report writer failed to
  save its response due to directory permissions; the result is established
  from the service log instead. No cookies or response HTML are committed.
- A separate Byte.to category control returned actual HTTP-200 HTML with 200
  ebook records in 5.83 seconds. This confirms browser transport, not Filecrypt
  or a downloadable EPUB.
- Runtime inspection confirmed no published ports, no paid provider and no OOM.
  RAM observed during the test was approximately 826 MiB under the 2 GiB limit.

The free Filecrypt path therefore remains unverified/blocked. Solverr is
installed as a diagnostic component; it has **not** been connected as an automatic
successful resolver in the acquisition worker. Existing failed/blocked job
counters and state were not reset. The application was not restarted for this
installation. Structured evidence: [test report](solverr-test-2026-10-31.json).
