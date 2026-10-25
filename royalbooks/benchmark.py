"""Opt-in, read-only source benchmark.

This module never downloads books. Every requested case is explicitly listed in
benchmarks/cases.json; no provider is continuously polled.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
from datetime import datetime, timezone
from pathlib import Path
from statistics import median
from time import perf_counter

import httpx

from royalbooks.matching import normalize
from royalbooks.providers import Book, Provider, ProviderError
from royalbooks.source_registry import configured_providers


def load_cases(path: Path) -> list[dict]:
    cases = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(cases, list) or not 1 <= len(cases) <= 100:
        raise ValueError("Expected 1 to 100 benchmark cases")
    for case in cases:
        if not isinstance(case, dict):
            raise ValueError("Case must be a JSON object")
        if not isinstance(case.get("query"), str) or not 2 <= len(case["query"]) <= 120:
            raise ValueError("Each case requires a 2–120 character query")
        if not isinstance(case.get("title"), str) or not 2 <= len(case["title"]) <= 150:
            raise ValueError("Each case requires an expected title")
        if case.get("language") not in ("de", "en", None):
            raise ValueError("language must be de, en, or null")
        if "author" in case and not isinstance(case["author"], str):
            raise ValueError("author must be text")
    return cases


def is_target(book: Book, case: dict) -> bool:
    """Conservative metadata match: name plus optional author and *known* language.

    Missing/unknown language is never silently treated as a language match.
    """
    wanted_title = normalize(case["title"])
    title = normalize(book.title)
    # Some catalogs append subtitles; do not accept arbitrary short substrings.
    if not wanted_title or not (title == wanted_title or title.startswith(wanted_title + " ")):
        return False
    lang = case.get("language")
    if lang and book.language != lang:
        return False
    if case.get("author"):
        author = normalize(case["author"])
        candidates = [normalize(a) for a in book.authors]
        if not any(a == author or a.startswith(author + " ") for a in candidates):
            return False
    return True


async def probe(provider: Provider, case: dict, client: httpx.AsyncClient, limit: int = 10) -> dict:
    start = perf_counter()
    try:
        books = await asyncio.wait_for(
            provider.search(client, case["query"], case.get("language"), limit),
            timeout=20.0,
        )
        target = [book for book in books if is_target(book, case)]
        return {
            "provider": provider.name,
            "status": "ok" if books else "empty",
            "found": len(books),
            "target_matched": bool(target),
            "target_candidates": len(target),
            # This is an unverified catalog link, NOT an actual tested download.
            "catalog_epub_links": sum(bool(book.epub_url) for book in target),
            "elapsed_ms": int((perf_counter() - start) * 1000),
        }
    except Exception as exc:
        return {
            "provider": provider.name,
            "status": "error",
            # A safe, non-secret provider status (e.g. http_403) is useful for auditing.
            "error_code": str(exc)[:80] if isinstance(exc, ProviderError) else type(exc).__name__,
            "found": 0,
            "target_matched": False,
            "target_candidates": 0,
            "catalog_epub_links": 0,
            "elapsed_ms": int((perf_counter() - start) * 1000),
        }


async def run_benchmark(cases: list[dict], providers: list[Provider], client: httpx.AsyncClient, limit: int = 10) -> dict:
    raw = []
    # Sequential cases respect the publishers' rate limits; not a bulk scraper.
    for case in cases:
        checks = []
        for provider in providers:
            checks.append(await probe(provider, case, client, limit))
        raw.append({"query": case["query"], "expected_title": case["title"], "language": case.get("language"),
                    "checks": checks})
    totals = []
    for provider in providers:
        attempts = [check for row in raw for check in row["checks"] if check["provider"] == provider.name]
        n = len(attempts)
        totals.append({
            "provider": provider.name,
            "queries": n,
            "successful_responses": sum(c["status"] != "error" for c in attempts),
            "errors": sum(c["status"] == "error" for c in attempts),
            "target_matches": sum(c["target_matched"] for c in attempts),
            "target_match_rate": round(sum(c["target_matched"] for c in attempts) / n, 3) if n else 0,
            "catalog_epub_link_cases": sum(c["catalog_epub_links"] > 0 for c in attempts),
            "median_response_ms": round(median(c["elapsed_ms"] for c in attempts)) if n else None,
        })
    return {
        "generated_at_utc": datetime.now(timezone.utc).isoformat(),
        "source": "opt_in_live_catalog_metadata",
        "notice": "Metadata matches and catalog EPUB links are not verified downloads, nor a grant of rights.",
        "cases_count": len(cases),
        "summary": totals,
        "cases": raw,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="Read-only RoyalBooks catalog benchmark; network opt-in required")
    parser.add_argument("--live", action="store_true", help="Explicitly authorize public catalog API queries")
    parser.add_argument("--cases", type=Path, default=Path("benchmarks/cases.json"))
    parser.add_argument("--output", type=Path, default=Path("data/provider-benchmark.json"))
    parser.add_argument("--limit", type=int, default=10)
    parser.add_argument("--providers", nargs="*", default=None, help="Provider names to probe")
    args = parser.parse_args()
    cases = load_cases(args.cases)
    registered = configured_providers()
    if args.providers:
        registered = [provider for provider in registered if provider.name in args.providers]
        unknown = set(args.providers) - {provider.name for provider in registered}
        if unknown:
            parser.error("Unknown or disabled providers: " + ", ".join(sorted(unknown)))
    if not registered:
        if args.live:
            parser.error("Public-catalog benchmark unavailable in indexer mode")
        print("DRY RUN: no public catalog providers enabled (indexer-first mode).")
        return
    if not 1 <= args.limit <= 30:
        parser.error("--limit must be 1–30")
    if not args.live:
        print(f"DRY RUN: {len(cases)} explicit cases, {', '.join(p.name for p in registered)}.")
        print("Pass --live to actually query public catalogs. No EPUBs are downloaded.")
        return
    async def execute():
        async with httpx.AsyncClient(
            timeout=httpx.Timeout(7.0), follow_redirects=False,
            headers={"User-Agent": "RoyalBooks/0.3 (https://github.com/TimeLance89/RoyalBooks)"}
        ) as client:
            return await run_benchmark(cases, registered, client, args.limit)
    result = asyncio.run(execute())
    args.output.parent.mkdir(parents=True, exist_ok=True)
    # Write result atomically so interrupted reports aren't confused with complete runs.
    tmp = args.output.with_name(args.output.name + ".tmp")
    tmp.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    os.replace(tmp, args.output)
    print(json.dumps(result["summary"], ensure_ascii=False, indent=2))
    print(f"Report saved: {args.output}")


if __name__ == "__main__":
    main()
