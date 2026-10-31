# RoyalBooks → BookOrbit

## Architektur und belegte Schnittstellen

Geprüft gegen BookOrbit **v3.6.0**, die laufende NAS-Version. Der offizielle
[Book-Dock-Ablauf](https://bookorbit.app/book-dock) trennt Eingang, Metadatenprüfung
und Finalize. Ein fehlender Eingangsordner-Eintrag ist kein Importnachweis.

Quellcodebasis: [Tag v3.6.0](https://github.com/bookorbit/bookorbit/tree/v3.6.0).
Verwendete veröffentlichte Controller:

- `POST /api/v1/auth/login`: `username`, `password`, `clientKind: native`, `deviceLabel`.
- `POST /api/v1/auth/refresh`: rotierender `refreshToken` im Body.
- `GET /api/v1/health`, `GET /api/v1/book-dock/summary`, `GET /api/v1/book-dock/files`.
- `POST /api/v1/books/query`, `GET /api/v1/books/:id`, `GET /api/v1/books/:id/cover`,
  `GET /api/v1/books/files/:fileId/download`.

Die Authentifizierung bleibt serverseitig und im Speicher. Ein gemeinsamer Lock
verhindert parallele Anmeldungen/Refresh-Rennen. Abgelaufene Refresh-Sitzungen
führen einmalig zu einer neuen Anmeldung; fehlgeschlagene Anmeldungen haben eine
60-Sekunden-Pause. Keine Datenbank- oder JWT-Signing-Secrets werden verwendet.

`royalbooks/bookorbit.py` kapselt API, Bibliotheksanzeige, Dateiabruf und Übergabe.
Die PostgreSQL-Datenbank wird nicht angesprochen. Die alten Calibre-Lesemodule
bleiben ausschließlich für Altprüfungen und expliziten Nur-Lese-Kompatibilitätsbetrieb
(`ROYALBOOKS_LIBRARY_BACKEND=calibre-readonly`) erhalten; Downloads sind dort aus.
Der Standard und die Compose-Konfiguration verwenden ausschließlich BookOrbit.

## Sichere Übergabe und Zustände

1. Autorisierter Download; Dateigröße, ZIP/CRC/EPUB und gegebenenfalls Quell-MD5 prüfen.
2. SHA-256 und Staging speichern; Abschluss und Validierung separat protokollieren.
3. In Book Dock als `.royalbooks-UUID.part` exklusiv schreiben, `fsync`, Größe und
   vollständige EPUB nochmals prüfen. BookOrbit verarbeitet diese Endung nicht.
4. Dauerhafte SQLite-Reservierung mit eindeutigem SHA-256 und Dateinamen anlegen.
5. Fertige Datei per **atomarem Hardlink ohne Überschreiben** als `royalbooks-UUID.epub`
   veröffentlichen, temporären Namen entfernen und Verzeichnis synchronisieren.
   Ein gewöhnliches POSIX-Rename würde vorhandene Ziele überschreiben können.
6. Übergabe quittieren, dann Staging entfernen. Kein erneuter Versand bei fehlender
   Eingangsdatei, Neustart oder unklarer Übergabe. Unsichere Fälle: manuelle Prüfung.

DB-Status `delivered` bleibt aus Kompatibilitätsgründen erhalten und bedeutet nur
Übergabe. `library_transfer` unterscheidet Book Dock vorhanden, Verarbeitung,
bereit, Fehler, Import unbestätigt und Import bestätigt. Eine bestätigte Aufnahme
setzt einen **registrierten, über die BookOrbit-API geladenen identischen EPUB-Inhalt**
voraus. Gleicher Titel allein genügt nicht. BookOrbits `fileHash` wird nicht als
SHA-256 interpretiert. Umbenannte/geänderte Dateien oder mehr als drei Suchkandidaten
können unbestätigt bleiben; sie werden niemals deswegen erneut versandt.

`VERIFY_IMPORT=1` aktiviert begrenzte Hintergrundprüfungen und die manuelle Prüfung.
RoyalBooks finalisiert oder verändert keine BookOrbit-Bibliothek per API.
Auto-Fetch, Zielbibliothek/-ordner und Auto-Finalize werden in BookOrbit eingerichtet.
Ohne Auto-Finalize bleiben neue Bücher zur Prüfung in Book Dock. Unzuverlässige
Metadaten dürfen dort bewusst eine manuelle Entscheidung erfordern.

## Bestehendes NAS: Variante ohne BookOrbit-Neustart

Am 31.10.2026 festgestellt:

- Docker 29.6.2, Compose 5.1.3, BookOrbit v3.6.0.
- `/data` ist das vorhandene Volume `bookorbit_bookorbit_appdata`.
- Der tatsächliche Eingang ist `/data/book-dock`; `/books` ist separat eingebunden.
- RoyalBooks kann **nur diesen Unterordner** per `compose.bookorbit-volume.yaml`
  einbinden. Das App-Volume und das PostgreSQL-Volume bleiben unverändert.
- Der Docker-Subpath funktioniert; beide Container-UIDs wurden tatsächlich geprüft.

Gezielte Änderung bereits ausgeführt: ACL für UID 10001 und Default-ACLs für
UID 10001/1000 auf **dem Book-Dock-Ordner allein**. Vorherige ACL:
`/volume1/docker/royalbooks/backups/bookorbit-20261031/dock-acl-before.txt`.
Kein rekursives chmod, kein 777, kein BookOrbit-/CWA-Neustart und keine Änderung
der alten Bibliothek. Rücknahme bei Bedarf mit `setfacl --restore=...` nach Prüfung
der gesicherten absoluten Pfade.

### Noch erforderlicher Produktivwechsel

1. Separaten Benutzer `royalbooks` in BookOrbit erstellen. Zugriff nur auf die
   vorgesehenen Bibliotheken und Dateien sowie Book Dock geben. Kein Superuser
   erforderlich; `ManageBookDock` nur für Verwaltungsfunktionen, die RoyalBooks
   nicht aufruft. Zugang separat in BookOrbit testen.
2. Secret-Datei auf dem NAS erstellen:

   ```sh
   cd /volume1/docker/royalbooks
   mkdir -p secrets
   chmod 700 secrets
   # Mit einem Editor erstellen; echtes Passwort nicht in Shell-History schreiben:
   # BOOKORBIT_USERNAME=royalbooks
   # BOOKORBIT_PASSWORD="..."
   chown 10001:10001 secrets secrets/bookorbit.env
   chmod 400 secrets/bookorbit.env
   ```

   Keine Shell-Auswertung oder Variablenersetzung. Nur diese beiden Schlüssel;
   wahlweise einfache/doppelte äußere Anführungszeichen. Datei nie committen.
3. SQLite konsistent per Backup-API sichern, ebenso Image, `.env` und Compose-Dateien.
   Erst ohne laufende Transfers umschalten. Kein Docker-`down`, kein Volume-Löschen.
4. Neue RoyalBooks-Dateien aus dem PR übernehmen und `.env` gezielt ergänzen:

   ```dotenv
   ROYALBOOKS_LIBRARY_BACKEND=bookorbit
   ROYALBOOKS_BOOKORBIT_URL=http://192.168.178.47:3100
   ROYALBOOKS_BOOKORBIT_PUBLIC_URL=http://192.168.178.47:3100
   ROYALBOOKS_BOOKORBIT_LIBRARY_PATH=/volume2/bookorbit-books
   ROYALBOOKS_BOOKORBIT_GID=1000
   ROYALBOOKS_SECRETS_PATH=./secrets
   ROYALBOOKS_BOOKORBIT_VERIFY_IMPORT=0
   ```

   Bei der Volume-Variante bleibt der **Host**-Wert `ROYALBOOKS_BOOKORBIT_INGEST_PATH`
   leer; das Overlay setzt den Containerpfad auf `/bookorbit-ingest`. Bestehende
   Such-, Token-, Hoster- und Indexer-Einstellungen erhalten. CWA-Variablen werden
   vom neuen Stack nicht mehr verwendet; seine alten Mounts entfallen.
5. Compose vorher prüfen, dann **nur RoyalBooks** neu erstellen. Bereits verwendete
   Indexer-/Hoster-Overlays beibehalten:

   ```sh
   docker compose -f compose.yaml -f compose.indexer.yaml -f compose.hosters.yaml -f compose.bookorbit-volume.yaml config --quiet
   docker compose -f compose.yaml -f compose.indexer.yaml -f compose.hosters.yaml -f compose.bookorbit-volume.yaml build royalbooks
   docker compose -f compose.yaml -f compose.indexer.yaml -f compose.hosters.yaml -f compose.bookorbit-volume.yaml up -d --no-deps royalbooks
   python3 scripts/nas_bookorbit_audit.py
   ```

   Die vorhandenen Hoster-/Solverr-Dienste werden weder entfernt noch neu erstellt.
6. Unter **Einstellungen → BookOrbit & Bibliothek** Integrationstest ausführen.
   Bibliotheksanzeige und EPUB-Abruf prüfen. Danach Importprüfung auf `1` setzen
   und einen freien Testtitel bis zum tatsächlichen Bibliothekseintrag verfolgen.
   Book Dock in BookOrbit **nicht** als normale Bibliothek registrieren.

Die interne Adresse `http://bookorbit-app:3000` ist nur mit dem geprüften externen
Netzwerk `bookorbit_default` verwendbar; dafür optional zusätzlich
`compose.bookorbit-network.yaml` einsetzen. Ohne dieses Overlay die NAS-Adresse nutzen.

## Alternative: neuer Host-Eingangsordner

Für `/volume2/bookorbit-inbox` zunächst Ordner und gezielte ACLs für beide UIDs
anlegen. Vor Änderungen prüfen, ob der bisherige Book Dock Dateien enthält:
ein neuer Bind-Mount würde sie verdecken. BookOrbits vorhandene Compose-Dateien
nicht überschreiben; [gesondertes Beispiel](bookorbit-stack-dock.override.yaml)
als zusätzliches Override prüfen. Es ergänzt nur `app` um den neuen Eingang.
Appdata-/PG-Volumes, Ports, Netzwerk und Bibliotheksmount bleiben erhalten.

Erst nach Freigabe des konkreten Overrides BookOrbit neu erstellen. RoyalBooks
bekommt denselben Hostpfad als `ROYALBOOKS_BOOKORBIT_INGEST_PATH`; das Volume-Subpath-
Overlay dann weglassen. Dieser alternative Stack-Umbau wurde **nicht** ausgeführt.

## Migration und Rückkehr

`jobs.library_backend` wird additiv ergänzt. Bereits ausgelieferte Altaufträge
bleiben Historie und werden nicht nach BookOrbit wiederholt. Frühere vorbereitete
Dateien werden zur manuellen Prüfung angehalten. Warteschlangen, Versuche,
Freigaben, Merkliste und Suchdaten bleiben erhalten. Entsprechender Schutz gilt
für historische Hoster-Staging-Aufträge.

Vor einem Rollback aktive Downloads und Übergaben anhalten. Ein altes Image kennt
das neue Übergabejournal nicht: **nicht einfach mit aktiven Workern und CWA-Mounts
starten**. Altes Image zunächst mit `ROYALBOOKS_DOWNLOADS_ENABLED=0` prüfen. Ein
DB-Backup nicht nach bereits erfolgten Book-Dock-Übergaben blind zurückspielen;
dadurch könnten Quittungen verloren gehen. CWA bleibt eine getrennte Altinstallation.

## Tests und tatsächlich ausgeführte NAS-Prüfung

Automatisierte Tests: Linux-Kandidat **328 bestanden** (vor drei zusätzlichen Fehlerfalltests), Windows **329 bestanden**, zwei bestehende Windows-Symlink-Skips. Fünf Browser-Suiten bestanden.

Mock- und Browserprüfungen: native Anmeldung, einzelner Refresh bei Parallelität,
abgelaufene Sitzungen, fehlende Secrets, API-Bibliothek/Dateiproxy, Rechtefehler,
Kollisionen, Duplikate, manipulierte/partielle EPUBs, kaputte OPF-Metadaten,
Stromausfall nach Veröffentlichung und additive Job-Migration. Screens bei
1440, 390 und 320 Pixeln; keine Calibre-Bezeichnungen in der aktiven Oberfläche.

Echte NAS-Prüfung in isolierter Testdatenbank: Gutenberg **Faust**, 191.405 Bytes,
SHA-256 `6e0ca94e1a76c247983f5e680c17f15763ace9dfade35714aa1b58e1788877ea`.
Job `e368e6363c314293a442ebf39a9e6482` wurde nach vollständiger Prüfung an
`/data/book-dock/royalbooks-e368e6363c314293a442ebf39a9e6482.epub` übergeben.
BookOrbits Container konnte die Datei als UID 1000 mit derselben Größe und SHA-256 lesen.
Kein Zugriff auf die produktive RoyalBooks-Queue, keine CWA-Übergabe.
Das bestätigt **Dateiübergabe**, keine Bibliotheksaufnahme. Echte Anmeldung und
Importbestätigung sind ausdrücklich bis zur Bereitstellung des Benutzers vertagt.

Reproduzierbar, standardmäßig ohne Netzwerk/Schreibzugriff:

```sh
python scripts/bookorbit_e2e.py
# Erst bei bewusst freigegebenem Test, in isolierten Daten, nicht in der Produktiv-Queue:
python scripts/bookorbit_e2e.py --live --rights-confirmed --data-dir /audit --ingest /bookorbit-ingest
```

Wiederholung mit derselben Testdatenbank versendet die Datei nicht erneut.
Der neue Produktionsstack wurde noch nicht umgeschaltet. Die vorhandene
RoyalBooks-Installation läuft bis zum oben beschriebenen Wechsel unverändert.
