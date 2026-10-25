# Source research — 2026-10-25

Read upstream READMEs, Git trees, license metadata and selected implementation
files through GitHub. This is a dated assessment, not a promise that external
sites remain available. No upstream implementation was copied in this change.
The pre-existing MIT `vendor/lgsearch.py` and its attribution remain intact.

## Reference projects

| Repository | License / last push observed | Interface and useful approach | Limitation / decision |
| --- | --- | --- | --- |
| [RoyalDownloader](https://github.com/TimeLance89/RoyalDownloader) | Apache-2.0 / Oct 24 | `source_resolution.py`, `source_service_health.py`: source orchestration, evidence-specific language checks and persistent lifecycle | Adopt separation of discovery, source health and acquisition; media resolvers themselves do not apply to books. |
| [Bindery](https://github.com/vavallee/bindery) | MIT / Oct 25 | `metadata_providers.go`, `docs/search-design.md`: independent Open Library/Google/Hardcover/DNB metadata, indexers and download clients | Metadata is separate from usable releases; cannot remove indexer infrastructure merely by using its metadata clients. |
| [Shelfarr](https://github.com/Pedro-Revez-Silva/shelfarr) | GPL-3.0 / Oct 24 | Acquisition-provider model separates metadata (Hardcover, Google, OL) from Prowlarr/Jackett/Newznab and direct sources (Anna, Z-Library, LibriVox) | Some source paths rely on accounts or optional challenge handling. Do not reproduce those mechanisms. |
| [Librarr](https://github.com/JeremiahM37/librarr) | MIT / Oct 19 | Parallel sources, format scoring, `internal/search/annas.go` parses HTML language/format segments separately | Site markup is fragile; format strings are claims, not verified file contents. German support depends on individual releases. |
| [ShelfBridge](https://github.com/selmant/shelfbridge) | MIT / Jul 14 | `internal/search/engine.go`: context deadlines, parallel LibGen/Anna/Z-Library; Torznab bridge | Requires service/API-key setup, configurable mirrors and potentially credentials. Useful orchestration, unsuitable as mandatory container. |
| [CWA Book Downloader](https://github.com/yatesco/calibre-web-automated-book-downloader) | MIT / Jan 14, 2025 | CWA ingest-directory workflow, web search and download queue | README advertises challenge bypass; not adopted. A file handed to ingest does not prove completed CWA import. |
| [Shelfmark](https://github.com/calibrain/shelfmark) | MIT / Oct 25 | Source/handler plugin interfaces, distinct metadata providers, explicit search deadlines and persistent download history | Multiple optional source/client setups; very long challenge budgets unsuitable for the required bounded keyless core. |
| [libgen-app](https://github.com/libgenapp/libgen-app) | MIT / Sep 15 | HTML tables, source MD5 identity, catalog-language and format fields; separate queue and integrity checks | Rotating origins and changing markup; no stable official API demonstrated. Existing parser retained, no new code copied. |
| [libgen-mcp](https://github.com/jmrplens/libgen-mcp) | MIT / Oct 22 | `internal/libgen/client.go`: source failover, rate limits, network guards; keyless open-access discovery/enrichment | Do not interpret a metadata hit as file authorization. Adapters should own protocol differences. |
| [OpenlibExtended](https://github.com/warreth/OpenlibExtended) | AGPL-3.0 / Oct 18 | `search_manager.dart`: common source interface, normalized filters, per-provider failures | Its Z-Library feasibility document describes incompatible markup and challenge solving. Those paths are deliberately excluded. |
| [Gutendex](https://github.com/garethbjohnson/gutendex) | MIT / Sep 26 | JSON API over Gutenberg RDF; explicit languages and MIME-to-URL mapping | Third-party service, can time out or refuse access. Official endpoint requires trailing slash. Gutenberg's US copyright flag does not settle rights elsewhere. |
| [sertraline/zlibrary](https://github.com/sertraline/zlibrary) | GPL-3.0 / Apr 2025 | Unofficial authenticated client; account sessions, pagination and download quotas | No proven supported anonymous public API; never required by RoyalBooks. |

All examined repositories were unarchived at inspection. Recent commits do not
establish source reliability or license compatibility for copied code. Distinct
licenses were checked before deciding to reuse concepts only.

## Actual source choices

| Source | Contract, language and files | Evidence / restrictions |
| --- | --- | --- |
| DNB | Public SRU/MARC21, title/author/ISBN search, edition ISBNs, 041/008 languages, 520 descriptions | Strong German bibliographic identification; no files. Preserve all valid ISBN-13s. Multiple declared languages stay ambiguous. |
| Open Library | Public Search JSON, work records/covers | Work-language sets span editions; no conversion to verified German edition and no work ISBN used as an edition ISBN. No downloads. |
| Wikisource WSIndex | Public JSON `books/?search=…&languages=de`; publisher-supplied WS Export URL | Live response has `credits=false`; previous adapter rejected every such link. German filtered catalog had only 12 rows, including multilingual records; coverage is narrow. Export errors must remain failures. |
| Standard Ebooks | Public newest-15 Atom feed, explicit EPUB enclosures | Live enclosures use `?source=feed`; previous adapter rejected them. English only, no full-catalog search promised. Descriptions come from feed summaries. |
| Gutendex | Public JSON `/books/`, EPUB MIME listings | Timeouts/refusals are independent. Origin refusal creates cooldown; no challenge handling. |
| Gutenberg official CSV/RDF | Optional local SQLite catalog populated from official bulk feeds | [Gutenberg](https://www.gutenberg.org/ebooks/offline_catalogs.html) provides bulk data. Its search HTML explicitly says not to scrape; no HTML scraper implemented. Imported RDF URLs are listings until downloaded and validated. |
| Library Genesis | Existing HTML table adapter, pinned HTTPS origins, EPUB/PDF/AZW3/etc. claims | An initial response was incompatible (`catalogue_table_missing`); the later recorded audit returned real listings, including German records. Explicit refusal/challenge stops fallback and initiates cooldown. Follow-up now implements fresh source-listed GET resolution and verified EPUB acquisition; see `libgen-acquisition.md`. |
| Google Books | Optional existing volume metadata API | API key remains optional, never required for core search. |
| Anna’s Archive | Public HTML scraping in examined clients; fast-download APIs may require membership/keys | No documented, stable, anonymous supported search-and-download API established. Not installed as a fictitiously working adapter. No authenticated download or CAPTCHA test performed. |
| Z-Library | Unofficial HTML/EAPI implementations, changing origins and account/session requirements | [Unofficial API documentation](https://github.com/baroxyton/zlibrary-eapi-documentation) describes personal domains and session headers. No anonymous authorized download integration verified; no bypass attempted. |

## Architecture decisions

One registry initializes independent adapters. The coordinator owns concurrency,
deadlines, bounded responses, result cache, in-flight coalescing and source health.
Adapters own parsing and download eligibility. Book records retain source IDs,
edition ISBNs, declared language evidence and explicit file evidence. Grouping
is a reversible display index; raw editions remain in the response.

Acquisition accepts source-bound identifiers, never arbitrary URLs. Existing
allowlisted redirects, size/ZIP checks, persistent retries, SHA-256 deduplication
and CWA staging are retained. Added CRC validation and deterministic database
connection closure. Old jobs default to unapproved in an additive migration.

See `source-audit-local.json`, NAS audit and the final test report for measured
results. Neither a blocked source nor a listed EPUB counts as a download test.
