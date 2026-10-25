# EPUB-Anbieter: GitHub- und NAS-Audit

Stand: 25.10.2026. Elf GitHub-Projekte anhand ihres Quellcodes geprüft. Die Netztests liefen vom RoyalBooks-NAS. Angaben im Quellcode und eigene Testergebnisse sind unten getrennt.

## Ergebnis für RoyalBooks

1. **Webshare ist ein realer zusätzlicher Dateiweg.** Anonyme API-Suchen und zwei vollständige EPUB-Abrufe funktionierten. Die deutsche Abdeckung ist noch nicht belegt: Die geprüfte Datei „Faust“ deklarierte intern `cs`, trotz eines deutsch wirkenden Dateinamens.
2. **EPUB.pub liefert öffentlich erreichbare EPUB-Bestandteile.** Bei zwei Büchern waren Container und OPF vorhanden. Das ist ein Rekonstruktionsverfahren. Eine vollständige Rekonstruktion aus 20 Ressourcen bestand anschließend die RoyalBooks-EPUB-Prüfung. Beide geprüften OPFs deklarieren `en`.
3. **Flibusta liefert einen echten OPDS-Katalog.** Startseite und OPDS-Startkatalog antworteten mit HTTP 200. Die erste OPDS-Suche lief in ein Timeout; ein vollständiger EPUB-Abruf ist damit noch nicht belegt.
4. **IRC/DCC erschließt einen anderen Transportweg.** Beide geprüften IRC-Netze akzeptierten eine TCP-Verbindung auf Port 6667. Bot-Suche und DCC-Dateitransfer sind noch nicht verifiziert.
5. **Randombook ist Mirror-Ermittlung, kein zusätzlicher Buchbestand.** JSON-Abfragen funktionierten; die ermittelten Hosts lieferten beim getesteten klassischen `ads.php`-Verfahren keinen passenden GET-Link.
6. Viele vermeintlich zusätzliche Anbieter teilen denselben Bestand. Insbesondere Sakes „Anna“-Provider durchsucht und lädt tatsächlich über LibGen. Solche Aliase oder Mirrors sollten nicht als unabhängige Anbieter gezählt werden.

## Quellen, URLs und Verfahren

| Quelle / URL | Verfahren im geprüften Projekt | Eigener NAS-Test | Bewertung |
|---|---|---|---|
| [Webshare](https://webshare.cz/) | POST-XML-API `https://webshare.cz/api/search/`; Dateiauflösung über `/api/file_link/`; temporäre Datei-URL auf `*.dl.wsfiles.cz`. Shelfmarks Client verwendet eine Sitzung. | Suche und Dateiauflösung funktionierten auch anonym. Zwei Abrufe derselben 303.624-Byte-EPUB: MIME, ZIP-CRC und OPF korrekt, gleiche SHA-256; OPF-Sprache `cs`. Ein späterer API-Aufruf hatte einen Verbindungs-Timeout. | Zusätzlicher Kandidat; Sprachprüfung und Fehlerbehandlung erforderlich. |
| [EPUB.pub](https://www.epub.pub/) / `https://asset.epub.pub/epub/{Dateiname}.epub/` | Reader-Verweis ermitteln; `META-INF/container.xml` lesen; daraus OPF und Manifest; Ressourcen laden und lokal als EPUB packen. | Zwei Container und OPFs jeweils HTTP 200. Ein Manifest hatte 20 Ressourcen / 17 XHTML-Einträge, das andere 67 / 59. Beide Sprache `en`. Eine zusätzliche vollständige Rekonstruktion der 20-Ressourcen-Probe ergab eine valide EPUB mit 1.026.072 Bytes. | Dateiverfahren vollständig an einer Probe bestätigt. Aktueller Suchparser, weitere Stichproben und deutsche Abdeckung bleiben separat zu prüfen. |
| [ReadAnyBook](https://www.readanybook.com/) | Der untersuchte Handler liest eine Ressourcen-URL aus `div.links-row[data-link]`; danach ebenfalls Rekonstruktion. | DNS-Auflösung scheiterte am NAS. | Aktuell nicht erreichbar; alten Parser nicht ungeprüft übernehmen. |
| [Flibusta](https://flibusta.is/) | HTML-Suche `/booksearch?ask={Suchtext}`; OPDS unter `/opds`; im Projekt formatbezogene Buchlinks `/b/{ID}/epub`. | Startseite und OPDS-Startfeed HTTP 200. Eine Goethe-Suchnavigation antwortete ebenfalls mit Atom/XML; weitere Autoren-/Buchabfragen liefen wiederholt in Timeouts von 12–20 Sekunden. | Katalog erreichbar, Dateiweg noch nicht bestätigt. Keine bestätigte deutsche Abdeckung. |
| [Randombook](https://randombook.org/) | MD5 über `/api/search/by-id?id={MD5}` auflösen; `/api/download/links-by-id?id={ID}` liefert Hosts und Landing-URLs. | Beide JSON-Endpunkte HTTP 200. Für die Probe u. a. `libgen.fyi`, `libgen.stream`, `freescienceengineering.org`. Auf diesen Hosts kein passender klassischer GET-Link. | Discovery/Fallback, keine bestätigte neue EPUB-Dateiquelle. Landing-URL ist kein direkter Downloadnachweis. |
| Anna’s Archive | `/md5/{MD5}` für Metadaten und ggf. IPFS-CID; Mitglieder-API `/dyn/api/fast_download.json`; alternativ quellseitig gelistete Mirrors. | Die zuvor geprüften Such-Mirrors blockierten. Auch die zusätzlich geprüfte MD5-Seite lieferte HTTP 403 und keine CID. | IPFS-Fallback ohne erreichbare CID-Seite nicht nachgewiesen. Mitgliederschlüssel ist keine kostenlose Voraussetzung, die man voraussetzen dürfte. |
| [WeLib](https://welib.org/) | Quellseitige `/auto_download/{MD5}/0/0`-Links und Buchseiten `/md5/{MD5}`; Shelfmark behandelt WeLib als Download-Mirror zu gefundenen Treffern. | Startseite und eine MD5-Seite HTTP 200; Suche HTTP 403. Der getestete auto_download-Link führte zurück zur Buchseite, nicht direkt zu einer EPUB. | Keine vollständige anonyme Such-/EPUB-Kette bestätigt. |
| [OceanofPDF](https://oceanofpdf.com/) | WordPress-Suche `/?s={Suchtext}`; Buchseite mit Format-POST an `Fetching_Resource.php`; Zwischen-HTML mit Meta-Refresh auf signierte Datei-URL. | HTTP 403 mit Browser-Challenge. | Projektintegration vorhanden, im eigenen Test blockiert. Keine Challenge-Umgehung getestet. |
| [Z-Library](https://z-lib.gl/) | Sake konfiguriert diese Basis-URL. Sitzung über `remix_userid` / `remix_userkey`; Suche `/eapi/book/search`; Dateiinfo `/eapi/book/{ID}/{Hash}/file`. | Homepage HTTP 503 mit Browserprüfung; keine angemeldete Suche und kein Dateiabruf geprüft. | Sitzungsabhängige Integration; nicht als getesteter anonymer Provider ausgeben. |
| [IRCHighway](https://irc.irchighway.net/) | IRC `irc.irchighway.net`, Kanal `#ebooks`; Suchbot liefert Ergebnisliste, gewählte Datei über DCC. | TCP 6667 erreichbar; TLS 6697 scheiterte an der Zertifikatsprüfung. Keine Bot-Abfrage oder DCC-Übertragung ausgeführt. | Eigener Transport-Adapter und Transferprüfung erforderlich. |
| Undernet | `irc.undernet.org:6667`, Kanal `#bookz`; IRC-Suche und DCC. | TCP-Verbindung erfolgreich; noch kein Datei-Ende-zu-Ende-Test. | Zweiter IRC-Netzweg; keine zusätzliche Dateiabdeckung allein durch den Porttest belegt. |
| [EBookBay](https://ebb.la/) | Öffentlicher Torrent-Indexer. WordPress-Suche `/?s={Suchtext}`; Detailseite liefert Torrent-Verweise. | HTTPS-Zertifikat passte nicht zu `ebb.la`. | Kein HTTPS-Test erfolgreich; Torrent-Client und Schwarmprüfung kämen zusätzlich hinzu. |
| [Bibliotik](https://bibliotik.me/) | Privater Torrent-Tracker, im Prowlarr-Indexer mit Login. | HTTP 200, Loginseite. | Kein anonymer EPUB-Abruf getestet. |
| [Booktracker](https://booktracker.org/) | Im Prowlarr-Indexer als halbprivater Tracker mit Sitzung und Torrent-Downloads umgesetzt. | Startseite HTTP 200. Keine angemeldete Suche oder Dateiübertragung geprüft. | Kein direkter HTTP-EPUB-Provider. |
| [MobileRead](https://wiki.mobileread.com/wiki/Free_eBooks-de/de) | Deutschsprachige Wiki-Liste → Forum-Thread → `https://www.mobileread.com/forums/attachment.php?attachmentid={ID}`. | Wiki-Abruf im Zeitbudget abgebrochen. | Zusätzliche Archivquelle; alter Downloader von 2017 ist kein Funktionsnachweis für heute. |
| Prowlarr / Jackett / Newznab | Adapter für Indexer und Download-Clients; Torrent/NZB-Ergebnisse werden anschließend beschafft und ggf. entpackt. | Kein neuer Indexer oder Client eingerichtet. | Infrastruktur, nicht jeweils ein eigener kostenloser Buchanbieter. |
| Soulseek / slskd | Shelfmark nutzt eine selbst gehostete slskd-API für Peer-Suche und Transfers. | Kein Dienst oder Peer-Transfer getestet. | Weitere Transportklasse; Peer-Verfügbarkeit und Sprache separat prüfen. |

## Reproduzierbare Prüfergebnisse

- Webshare-Datei: ID `BrkqlWJ2tv`, Titel im OPF „Faust“, deklarierte Sprache `cs`, 303.624 Bytes.
- Zwei Abrufe lieferten denselben SHA-256: `e75c7f5971b5e5476e352a99e612866fda603df97f5651bd31d689fbe657de16`.
- Webshare-Suchen lieferten auch andere Medien. Ein Suchtreffer oder deutsch wirkender Dateiname ist keine gesicherte deutsche EPUB.
- EPUB.pub: Die aktuelle Probe hatte OPF-Titel „Assassin: Part 1 (Veil Syndicate Series)“, Sprache `en`, 67 Manifest-Ressourcen. Eine ältere Probe hatte „It“, Sprache `en`, 20 Ressourcen.
- Zusätzlicher EPUB.pub-Dateitest: 20 Manifest-Ressourcen / 2.908.376 Bytes abgerufen, temporär zu einer 1.026.072-Byte-EPUB zusammengesetzt. RoyalBooks-Validierung bestanden; SHA-256 `53a7c89bb89aa1944f8a845ca9edde2bfeb3366cb6a7d634bfada9344d6ecc0b`. Das temporäre Archiv wurde nach der Prüfung entfernt.
- Webshare-Testdateien wurden im Arbeitsspeicher geprüft; der Rekonstruktionstest verwendete ein temporäres Verzeichnis. Es erfolgte kein Calibre-Import. Keine RoyalBooks-Downloadaufträge wurden für diesen Audit angelegt.

## Geprüfte Projekte und Quellcode

| Projekt | Geprüfter Stand | Relevanter Quellcode |
|---|---|---|
| Shelfmark | d50440f6 · 2026-10-25 | [Provider-Registry](https://github.com/calibrain/shelfmark/blob/d50440f6c28b31d304795bcd9be86b3715aa13cd/shelfmark/release_sources/direct_download/registry.py), [OceanofPDF](https://github.com/calibrain/shelfmark/blob/d50440f6c28b31d304795bcd9be86b3715aa13cd/shelfmark/release_sources/direct_download/oceanofpdf.py), [Webshare-API](https://github.com/calibrain/shelfmark/blob/d50440f6c28b31d304795bcd9be86b3715aa13cd/shelfmark/release_sources/webshare/api.py) |
| libgen-mcp | 4ed682ba · 2026-10-22 | [Randombook](https://github.com/jmrplens/libgen-mcp/blob/4ed682ba41d367dad588d3775e8a2670f6fd6970/internal/libgen/source_randombook.go), [Anna/IPFS](https://github.com/jmrplens/libgen-mcp/blob/4ed682ba41d367dad588d3775e8a2670f6fd6970/internal/libgen/source_annas.go) |
| epub_downloader | c50cc685 · 2025-05-15 | [EPUB.pub-Locator](https://github.com/mcombeau/epub_downloader/blob/c50cc685c693d3197b79639d8ba6231937db2ecb/src/epub_locator/handlers/epub_pub_handler.py), [Rekonstruktion](https://github.com/mcombeau/epub_downloader/blob/c50cc685c693d3197b79639d8ba6231937db2ecb/src/epub_file_downloader/epub_file_downloader.py) |
| CWA Book Downloader, lieu4d-Fork | c8f21b8f · 2025-08-26 | [Mirror-Konfiguration](https://github.com/lieu4d/calibre-web-automated-book-downloader/blob/c8f21b8f8d94757fdb9a87ebd4c8fef26523777f/config.py), [Downloadlogik](https://github.com/lieu4d/calibre-web-automated-book-downloader/blob/c8f21b8f8d94757fdb9a87ebd4c8fef26523777f/downloader.py) |
| Sake | 8d329aca · 2026-10-10 | [„Anna“ über LibGen](https://github.com/Sudashiii/Sake/blob/8d329aca4e482e777df42400f8bc277689ad5285/sake/src/lib/server/infrastructure/search-providers/AnnaArchiveSearchProvider.ts), [Z-Library-Client](https://github.com/Sudashiii/Sake/blob/8d329aca4e482e777df42400f8bc277689ad5285/sake/src/lib/server/infrastructure/clients/ZLibraryClient.ts) |
| Prowlarr Indexers | 5482e112 · 2026-10-25 | [EBookBay](https://github.com/Prowlarr/Indexers/blob/5482e1129828d47f7b1d2e917f2a6739285fbae8/definitions/v11/ebookbay.yml), [Bibliotik](https://github.com/Prowlarr/Indexers/blob/5482e1129828d47f7b1d2e917f2a6739285fbae8/definitions/v11/bibliotik.yml), [Booktracker](https://github.com/Prowlarr/Indexers/blob/5482e1129828d47f7b1d2e917f2a6739285fbae8/definitions/v11/booktracker.yml) |
| LazyLibrarian, DobyTang-Stand | 45d4f24d · 2018-12-03 | [Direktparser](https://github.com/DobyTang/LazyLibrarian/blob/45d4f24da214507c6913bde47efe063688d6bb84/lazylibrarian/directparser.py). Legacy-Stand; alte `libgen.io`-Verweise nicht als aktuelle Quellen behandeln. |
| MR-eBook-Downloader | 16897b08 · 2017-01-21 | [Wiki-/Thread-/Attachment-Verfahren](https://github.com/abbyarmada/MR-eBook-Downloader/blob/16897b081032c7eb7e3b5bc3ed47d8a656f15053/src/MrDeDownloader.py) |
| OpenBooks | ad12382c · 2025-04-22 | [IRC-Suche/Download](https://github.com/evan-buss/openbooks/blob/ad12382c4c00349fa596c45db053d6ed5f56dad5/core/irchighway.go), [Betriebshinweise](https://github.com/evan-buss/openbooks/blob/ad12382c4c00349fa596c45db053d6ed5f56dad5/docs/docs/irc-notes.md) |
| book | 07f7dae9 · 2026-01-03 | [IRCHighway und Undernet](https://github.com/liamg/book/blob/07f7dae9455760bcbe216eadafe16954442329f9/README.md) |
| flibusta-downloader | 43ab1952 · 2026-08-17 | [Buch-/Format-Auflösung](https://github.com/Ivankolp/flibusta-downloader/blob/43ab19529126c39ffb5ac4b1a1a1bb896894656d/flibusta_downloader.py) |

Die Projektanzahl ist keine Anbieteranzahl. Forks, Mirrors und Aliase sind teilweise derselbe Downloadweg. Die zahlreichen Forschungsquellen von libgen-mcp liefern überwiegend wissenschaftliche PDF-Dateien und dürfen nicht als zusätzliche Roman-EPUB-Anbieter gezählt werden.

## Technische Ausbaufolge

1. **Webshare-Adapter isoliert erproben:** Dateityp filtern, Sprache aus der EPUB prüfen, temporäre Links frisch auflösen, Fehler/Timeouts erfassen. Erst eine belastbare Titel-/Sprach-Stichprobe rechtfertigt automatische Angebote.
2. **EPUB.pub-Rekonstruktion isoliert erproben:** Aktuellen Locator statt des alten `a.btn-read`-Selektors verwenden; Ressourcen auf den Buchpfad begrenzen; ZIP-Pfade, Größe und Vollständigkeit prüfen. Der alte Selector fehlt auf den heute geprüften Buchseiten.
3. **Flibusta-OPDS und IRC/DCC separat bewerten:** Zusätzliche Protokolle benötigen eigene Zeitbudgets, Transferprüfung und Verfügbarkeitsstatus.
4. **Treffer auf Dateiebene zusammenführen:** gleiche Datei/MD5 über verschiedene Mirrors nicht mehrfach anbieten; Sprache, Ausgabe und tatsächliche Dateiprüfung bleiben maßgeblich.

