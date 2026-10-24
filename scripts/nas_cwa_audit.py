#!/usr/bin/env python3
"""Read-only host-side audit of REAL Docker mounts for RoyalBooks + CWA.

No Docker socket is mounted into RoyalBooks. Run this *on the NAS host* with an
account allowed to inspect Docker. Only container identity, state and mounts
are queried; no environment variables, secrets, logs or files are read.
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from pathlib import Path

CWA_INGEST = "/cwa-book-ingest"
CWA_LIBRARY = "/calibre-library"
RB_INGEST = "/cwa-ingest"
RB_LIBRARY = "/cwa-library"
RB_DATA = "/data"


def _result(name: str, level: str, detail: str) -> dict[str, str]:
    return {"name": name, "level": level, "detail": detail}


def _same_path(a: str, b: str) -> bool:
    if not a or not b:
        return False
    return os.path.normcase(os.path.realpath(a)) == os.path.normcase(os.path.realpath(b))


def _overlap(a: str, b: str) -> bool:
    if not a or not b:
        return False
    try:
        a, b = os.path.realpath(a), os.path.realpath(b)
        return os.path.commonpath([a, b]) in (a, b)
    except ValueError:
        return False


def _mounts_for(container: dict, mounts_by_id: dict) -> dict:
    mounts = mounts_by_id.get(container["id"], [])
    return {m["Destination"]: m for m in mounts
            if isinstance(m, dict) and isinstance(m.get("Destination"), str)
            and isinstance(m.get("Source"), str)}


def _source(mount: dict | None) -> str | None:
    return mount.get("Source") if mount else None


def inspect_snapshot(containers: list[dict], mounts_by_id: dict,
                     *, cwa_name: str | None = None, royalbooks_name: str | None = None) -> dict:
    """Pure analysis for unit testing; makes no Docker or filesystem calls."""
    checks = []

    def identify(label: str, override: str | None, predicate):
        if override:
            matches = [c for c in containers if c.get("name") == override or c.get("id") == override]
        else:
            matches = [c for c in containers if predicate(c)]
        if len(matches) > 1:
            checks.append(_result(label + "_discovery", "error",
                                  "Mehrere mögliche Container gefunden; Namen explizit angeben."))
            return None
        if not matches:
            checks.append(_result(label + "_discovery", "warning",
                                  "Container nicht gefunden."))
            return None
        return matches[0]

    cwa = identify("cwa", cwa_name, lambda c: any(word in (
        c.get("name", "") + " " + c.get("image", "")).lower()
        for word in ("calibre-web-automated", "calibre_web_automated")))
    rb = identify("royalbooks", royalbooks_name, lambda c: "royalbooks" in (
        c.get("name", "") + " " + c.get("image", "")).lower())
    report = {
        "status": "blocked",
        "containers": {
            "cwa": {"name": cwa["name"], "state": cwa["state"]} if cwa else None,
            "royalbooks": {"name": rb["name"], "state": rb["state"]} if rb else None,
        },
        "mounts": {},
        "checks": checks,
        "changes_made": False,
        "notice": ("Das ist ein reiner Mount-Abgleich. Er bestätigt weder das "
                   "Auslösen des CWA-Watchers noch einen Buchimport."),
    }
    if not cwa:
        checks.append(_result("cwa_required", "error", "Laufenden CWA-Container ermitteln."))
        return report
    if cwa["state"] != "running":
        checks.append(_result("cwa_running", "error", "CWA ist nicht gestartet."))
    else:
        checks.append(_result("cwa_running", "ok", "CWA-Container läuft."))

    cm = _mounts_for(cwa, mounts_by_id)
    ingest, library = cm.get(CWA_INGEST), cm.get(CWA_LIBRARY)
    report["mounts"]["cwa_ingest_host"] = _source(ingest)
    report["mounts"]["cwa_library_host"] = _source(library)
    if not ingest:
        checks.append(_result("cwa_ingest", "error", "CWA-Mount /cwa-book-ingest fehlt."))
    elif not ingest.get("RW"):
        checks.append(_result("cwa_ingest", "error", "CWA-Ingest ist read-only."))
    else:
        checks.append(_result("cwa_ingest", "ok", "CWA hat ein beschreibbares Ingest-Mount."))
    if not library:
        checks.append(_result("cwa_library", "error", "CWA-Mount /calibre-library fehlt."))
    else:
        checks.append(_result("cwa_library", "ok", "Calibre-Library-Mount vorhanden."))

    if ingest and library and _overlap(ingest["Source"], library["Source"]):
        checks.append(_result("cwa_mount_isolation", "error", "Ingest und Bibliothek überlappen auf dem Host."))

    if not rb:
        checks.append(_result("royalbooks_setup", "warning",
                              "RoyalBooks ist nicht gestartet; bisher nur CWA-Pfade erkannt."))
        report["status"] = "blocked" if any(c["level"] == "error" for c in checks) else "needs_setup"
        return report
    if rb["state"] != "running":
        checks.append(_result("royalbooks_running", "error", "RoyalBooks ist nicht gestartet."))
    else:
        checks.append(_result("royalbooks_running", "ok", "RoyalBooks-Container läuft."))

    rm = _mounts_for(rb, mounts_by_id)
    rb_ingest, rb_library, rb_data = rm.get(RB_INGEST), rm.get(RB_LIBRARY), rm.get(RB_DATA)
    report["mounts"].update(royalbooks_ingest_host=_source(rb_ingest),
                            royalbooks_library_host=_source(rb_library),
                            royalbooks_data_host=_source(rb_data))
    if not rb_ingest or not ingest:
        checks.append(_result("shared_ingest", "error", "Der Ingest-Mount fehlt in mindestens einem Container."))
    elif not _same_path(rb_ingest["Source"], ingest["Source"]):
        checks.append(_result("shared_ingest", "error",
                              "RoyalBooks und CWA verwenden UNTERSCHIEDLICHE Host-Ingest-Ordner."))
    elif not rb_ingest.get("RW"):
        checks.append(_result("shared_ingest", "error", "RoyalBooks-Ingest ist read-only."))
    else:
        checks.append(_result("shared_ingest", "ok", "Beide Container verwenden denselben Host-Ingest-Ordner."))

    if not rb_library or not library:
        checks.append(_result("shared_library", "warning",
                              "Read-only Calibre-Verifikation nicht vollständig gemountet."))
    elif not _same_path(rb_library["Source"], library["Source"]):
        checks.append(_result("shared_library", "error",
                              "RoyalBooks zeigt auf eine andere Bibliothek als CWA."))
    elif rb_library.get("RW"):
        checks.append(_result("shared_library", "error",
                              "RoyalBooks-Bibliotheksmount muss strikt read-only sein."))
    else:
        checks.append(_result("shared_library", "ok", "Calibre-Bibliothek ist bei RoyalBooks read-only eingebunden."))

    if not rb_data or not rb_data.get("RW"):
        checks.append(_result("royalbooks_storage", "error", "/data ist nicht beschreibbar eingebunden."))
    else:
        checks.append(_result("royalbooks_storage", "ok", "RoyalBooks /data ist beschreibbar gemountet."))
        for dest, mount in (("ingest", ingest), ("library", library)):
            if mount and _overlap(rb_data["Source"], mount["Source"]):
                checks.append(_result("storage_isolation_" + dest, "error",
                                      "RoyalBooks /data darf nicht mit CWA-" + dest + " überlappen."))
    if any(c["level"] == "error" for c in checks):
        report["status"] = "blocked"
    elif any(c["level"] == "warning" for c in checks):
        report["status"] = "review_required"
    else:
        report["status"] = "mounts_match_not_import_verified"
    return report


def _docker(*args: str) -> str:
    try:
        done = subprocess.run(["docker", *args], check=False, capture_output=True,
                              text=True, timeout=15)
    except (FileNotFoundError, subprocess.TimeoutExpired) as exc:
        raise RuntimeError("Docker CLI fehlt oder antwortet nicht.") from exc
    if done.returncode != 0:
        # Docker stderr can contain environment-specific names; don't echo arbitrary text.
        raise RuntimeError("Docker-Abfrage fehlgeschlagen. Benutzer benötigt Docker-Leserechte.")
    return done.stdout


def read_snapshot() -> tuple[list[dict], dict]:
    listing = _docker("ps", "-a", "--format", "{{.ID}}\t{{.Names}}\t{{.Image}}\t{{.State}}")
    containers = []
    mounts = {}
    for row in listing.splitlines():
        parts = row.split("\t")
        if len(parts) != 4:
            continue
        ident, name, image, state = parts
        containers.append({"id": ident, "name": name, "image": image, "state": state})
    for container in containers:
        name = (container["name"] + " " + container["image"]).lower()
        if not any(word in name for word in ("calibre-web-automated", "calibre_web_automated", "royalbooks")):
            continue
        raw = _docker("inspect", "--format", "{{json .Mounts}}", container["id"])
        try:
            mounts[container["id"]] = json.loads(raw)
        except (json.JSONDecodeError, TypeError) as exc:
            raise RuntimeError("Ungültige Docker-Mount-Ausgabe.") from exc
    return containers, mounts


def main() -> int:
    parser = argparse.ArgumentParser(description="RoyalBooks / Calibre-Web Automated: hostseitiger Docker-Mount-Audit")
    parser.add_argument("--cwa-container", help="CWA-Containername, wenn mehrere Kandidaten existieren")
    parser.add_argument("--royalbooks-container", help="RoyalBooks-Containername, wenn mehrere Kandidaten existieren")
    parser.add_argument("--json", action="store_true", help="Strukturierten Bericht ausgeben")
    args = parser.parse_args()
    try:
        containers, mounts = read_snapshot()
        report = inspect_snapshot(containers, mounts, cwa_name=args.cwa_container,
                                  royalbooks_name=args.royalbooks_container)
    except RuntimeError as exc:
        print(f"FEHLER: {exc}", file=sys.stderr)
        return 2
    if args.json:
        print(json.dumps(report, ensure_ascii=False, indent=2))
    else:
        print("RoyalBooks ↔ Calibre-Web Automated: Docker-Mount-Prüfung (nur lesend)")
        for entry in report["checks"]:
            print(f"{entry['level'].upper():7} {entry['name']}: {entry['detail']}")
        print("\nHost-Pfade (keine Zugangsdaten):")
        for name, path in report["mounts"].items():
            print(f"  {name}: {path or '[nicht vorhanden]'}")
        print(f"\nErgebnis: {report['status']}")
        print("Kein Dateizugriff, kein EPUB-Download, kein Import, keine Änderungen.")
        print("HINWEIS: Vor dem Teilen des Berichts private Pfadnamen prüfen.")
    return 2 if report["status"] == "blocked" else 0


if __name__ == "__main__":
    raise SystemExit(main())
