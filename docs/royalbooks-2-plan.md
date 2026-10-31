# RoyalBooks 2.0 – implementation plan

Base: `main` at `81db3f1b7a59544f1849466ba60927113226a508` (2026-10-25).

## Findings

The primary endpoint currently searches one LibGen adapter sequentially across
four mirrors (up to 36 seconds); the central registry returns no providers.
Existing independent catalog adapters, conservative grouping, SQLite watchlist,
validated EPUB queue and read-only CWA verification are already implemented but
disconnected from primary discovery. Downloads require a legacy feature flag.
The README contradicts itself across accumulated milestones. No database reset,
library manipulation or additional container is necessary.

## Implementation sequence

1. Research reference repositories, licenses and concrete source interfaces;
   record evidence and limitations in `source-research.md`.
2. Create a development branch. Restore the central registry, introduce a
   bounded search coordinator with shared clients, concurrency limits, cache,
   single-flight queries, provider health and isolated failures. Normalize
   title/author/ISBN queries and preserve all edition identifiers.
3. Adapt the existing LibGen listing parser to the common Book contract. Retain
   catalog claims separately from file verification. Restore DNB, Open Library,
   Gutendex, local Gutenberg, Wikisource and Standard Ebooks adapters. Correct
   Wikisource against the actual WSIndex API (search parameter and explicitly
   listed `credits=false` exporter URLs). No access-control solvers or proxies.
4. Restore explicitly authorized EPUB acquisition without a legacy flag. Keep
   existing persistent jobs and mounts; process only newly consented v2 jobs
   automatically so an upgrade cannot release a historic backlog. Preserve
   the legacy flag for an operator who explicitly resumes old jobs.
5. Connect grouped cover cards, filters, source diagnostics, book details,
   watchlist and downloads to the common pipeline. Keep normal search tokenless
   and administration authenticated. Retain dark/gold RoyalBooks identity;
   use a serif book-title treatment with compact mobile controls.
6. Add coordinator, evidence, migration, API and security regression tests;
   exercise real sources with a reproducible read-only audit and one eligible
   publisher EPUB validation. Report failures as failures.
7. Run the full suite, JavaScript checks, desktop/mobile browser flows, security
   checks and GitHub Actions Docker build/runtime tests. Fix failures, then
   create the PR only after all required executable checks pass.

## Acceptance evidence

Live tests require no external provider credentials. Metadata success is never
download success. German coverage is measured per provider, including failures.
Docker is unavailable on this Windows host; Linux CI must build and start the
actual non-root image. The NAS/CWA service is not assumed accessible: a simulated
handoff or CI smoke test cannot prove an import on the user's real NAS.
