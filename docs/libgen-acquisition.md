# Free LibGen acquisition — follow-up

The original implementation had a material gap: LibGen catalog records could
contain EPUB format claims, but no acquisition adapter existed. The browser's
“Mit EPUB-Link” filter also hid these records. A successful English Standard
Ebooks test did not establish support for the user's actual shadow-library use.

## Implemented

- LibGen is scheduled before metadata providers. Real database-overload notices
  get one bounded retry instead of being mislabeled as a 30-minute access ban.
  Actual refusals/challenges are still respected.
- Search stores an expiring, server-bound acquisition ID only for EPUB listings.
  Historic MD5 source identities and watchlist entries remain unchanged.
- A job fetches the selected catalog's actual `ads.php` page, parses its actual
  same-origin `get.php` anchor and uses that freshly provided key. No key is
  guessed, persisted or returned to the browser.
- Transfers allow only pinned HTTPS catalog hosts and the observed BooksDL CDN
  host family. Every redirect is checked and must preserve the selected MD5.
  Foreign/private hosts, HTTP downgrades, unknown routes and switched files fail.
- Downloaded files must pass EPUB/ZIP/CRC, catalog MD5 and SHA-256 checks. The
  existing persistent queue, deduplication, rights consent and CWA handoff apply.
- The **EPUB-Dateieinträge** filter and card actions now include source-bound
  LibGen EPUBs without pretending an advertisement page is a direct EPUB URL.
  Relevant EPUB acquisition candidates rank ahead of bibliography-only entries.

The RoyalBooks admin token remains necessary for queue operations. This is not
a provider account, subscription, paid API key or token needed for ordinary search.

## Actual network evidence

- The user's query “Harry Potter und der Stein der Weisen” returned LibGen EPUB
  catalog entries on the NAS. These were previously hidden by the link-only
  filter and had no usable acquisition action. Listings alone are not verified
  transfers, and that title was not downloaded for this test.
- A full free-route transfer of Goethe's **Faust** succeeded: **1,927,093 bytes**,
  EPUB/CRC validation passed, catalog MD5 matched, SHA-256:
  `164c67c84439a5e48aed8f8f21dca2667b843c33c92a29d60aa014d4e3386ec8`.
  See [machine-readable result](libgen-live-download.json).
- The observed route was a source HTML GET anchor followed by an HTTPS 307
  redirect to `cdn2.booksdl.lc/get.php`. No account or payment, CAPTCHA solver,
  DRM removal, proxy circumvention or access-restriction workaround was used.

Tests cover selected-file/source binding, invalid targets, missing/challenge
pages, fresh key parsing, safe redirects, complete queue execution, MD5 mismatch,
API authentication/consent and browser submission of the acquisition ID.

NAS follow-up: **194 Linux tests passed**; Windows **193 passed, 1 symlink
privilege skip**. Desktop and mobile browsers passed, including the LibGen EPUB
filter and acquisition-ID submission. The real NAS query returned **15 LibGen
EPUB acquisition candidates** at limit 30; the ordinary UI shows up to 10 per
source. See [NAS search evidence](libgen-nas-search.json) and actual
[desktop](screenshots/libgen-desktop.png) / [mobile](screenshots/libgen-mobile.png).

The real NAS LibGen **Faust** job downloaded, validated and delivered on attempt
1. Calibre's matching title count changed **0 → 1**, and CWA logs identify the
exact job file as successfully imported. The existing strict identical-hash
presence check remains unverified after CWA rewrites the EPUB; this is not
overridden with an invented success. [NAS acquisition evidence](libgen-nas-acceptance.json).

## Remaining limits

This establishes a functioning free LibGen acquisition route, not guaranteed
availability of every catalog item or perpetual stability of warez sites.
Anna’s Archive and Z-Library have not been promoted to working download
providers without a demonstrated compatible route. PDF/AZW3/MOBI acquisition
remains outside the EPUB implementation.

Separately, the actual Gutenberg bulk import exposed a legitimate RDF record of
1,223,615 bytes, exceeding the old 1 MiB limit. That importer now stages parsing
outside the production database and accepts bounded records up to 16 MiB. This
fix is independent of the user's shadow-library requirement; Gutenberg is not
presented as a substitute for it.
