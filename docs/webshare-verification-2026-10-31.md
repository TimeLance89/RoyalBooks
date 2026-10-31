# Verified anonymous EPUB acquisition

Deployed to the NAS on 31 October 2026. Webshare is enabled alongside the
existing sources; saved source choices were preserved. No account or payment.

The source search is fuzzy, so RoyalBooks filters filenames, downloads at most
two new candidates per search and verifies their actual EPUB metadata. Only
matching titles/authors in the requested language are published. Completed
verified files are cached privately for 24 hours (32 records / 64 MiB maximum;
8 MiB per file). Queueing consumes those exact bytes and validates them again.
Expired/evicted files require a new search. Signed links never enter API results
or persistent metadata. Downloads and redirects remain bound to the source's
HTTPS file origins. Only ZIP mimetype ordering/compression is repaired, with
stable timestamps; archive limits and complete validation remain mandatory.

Protocol reference reviewed:
[Shelfmark Webshare adapter](https://github.com/calibrain/shelfmark/blob/main/shelfmark/release_sources/webshare/api.py).
Anonymous search and file-link requests were independently verified on the NAS.
The metadata-only benchmark excludes this provider because its searches fetch
complete files.

## Actual production results

| Title | Source file | Calibre book | Verified from library |
|---|---:|---:|---|
| Die Seelenburg — Jason Dark | 139,741 bytes after container repair | 17 | German EPUB, 138,962 bytes |
| Die Nilbraut — Georg Ebers | 902,402 bytes | 18 | German EPUB, 903,854 bytes |

The first job was created through Chromium's real search/download UI, with no
console errors. Both jobs completed on attempt one. Calibre's EPUB processing
changed the files; each was subsequently downloaded from `/api/library` and
validated independently, including title and language. Calibre briefly exposed
the second database row before its file became readable; verification resumed
after its ingest log confirmed completion. No repeat import was requested.
The mobile library view also shows the imported book without horizontal overflow.

Cold isolated search: Die Seelenburg 855 ms; Die Nilbraut 3,335 ms.
Faust returned no German file; checked Czech editions were correctly excluded.
This demonstrates two working downloads, not complete German catalog coverage.

Validation: existing Linux suite plus four initial acquisition tests: 304 passed.
An additional deterministic-repair regression passed locally with all five new
tests. Chromium platform tests passed at 1440/390/320 pixels; search smoke tests
passed on desktop/mobile. SQLite and the prior image were retained before the
production restart, with no active direct transfers.

## Remaining boundary

Byte.to/Filecrypt remains blocked by Cloudflare. Official alternative Filecrypt
domains also returned challenge responses. The isolated Solverr test did not
resolve the supplied container. Webshare is a separate working download source,
not a Filecrypt bypass or a guarantee that every requested book is available.
