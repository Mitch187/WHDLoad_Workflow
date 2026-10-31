# Changelog

Alle nennenswerten Änderungen an diesem Projekt. Format orientiert sich an
"Keep a Changelog", Versionierung an semantischen Versionen.

## [2.5.0] - 2026-10-31

Zusammenführung von 1.3 (Monolith, Standardbibliothek) und 2.0 (modular, Rich).
Ziel war die Oberfläche von 2.0 ohne jede externe Abhängigkeit, bei identischem
fachlichem Ergebnis.

### Hinzugefügt

- Einzeldatei `whdload_workflow.py` mit sechs klar getrennten Blöcken
  (Konfiguration, UI/Logging, Datei-Handling, FTP-Kern, Schritte, CLI).
- Eigene Terminal-Oberfläche ohne `rich`: 256-Farben und 24-Bit-Farben
  (abhängig von `COLORTERM`), Bold/Dim, runde Panels, Tabellen mit
  Spaltenausrichtung, Rules, Fortschrittsbalken mit KB-Anzeige (wie 2.0),
  Spinner für unbestimmte Dauer, Bestätigungsdialoge mit `[J/n]`.
- Dynamische Breite über `shutil.get_terminal_size()` inklusive korrekter
  Zählung von Emoji (zwei Zellen) und ANSI-Sequenzen (null Zellen).
- `argparse`-CLI mit farbiger Hilfe und den Befehlen `run`, `mirror`, `sort`,
  `compare`, `finalize`, `cleanup`, `info`, `--version`.
- `logging` aus der Standardbibliothek mit zwei Zielen: Datei (DEBUG, Format
  `YYYY-MM-DD HH:mm:ss | LEVEL    | Nachricht`) und stderr (WARNING, bei
  `--verbose` DEBUG). Jede Konsolenzeile wird als `[UI]`-Zeile gespiegelt.
- Sicherheitsguards: Cleanup löscht nur innerhalb von `workdir`, `final` und
  `logs` sind gesperrt, Quelle-gleich-Ziel wird abgelehnt.
- Deutsche Kommentare und Docstrings, die auch die WHDLoad- und
  ANSI-Eigenheiten erklären.
- `requirements.txt`, die nur dokumentiert, dass keine Pakete nötig sind.
- `.gitignore` für `workdir/`.

### Geändert

- `workdir/`-Struktur aus 2.0 übernommen: `mirror/`, `sort/`, `final/`, `logs/`.
  Die Ordner `whdload-mirror/`, `whdload-sort/` und `whdload-final/` aus 1.3
  entfallen.
- Statistiken laufen als Dataclasses statt als globale Variablen.
- Dateilisten und FTP-Listings behalten die Reihenfolge von Server und
  Dateisystem (wie 1.3/2.0). Ein zwischenzeitliches Umsortieren wurde
  zurückgenommen, weil es den Sprach-Fallback verändert hat.

### Entfernt

- Abhängigkeiten `rich`, `typer`, `loguru`, `pytest`.
- Modulstruktur aus 2.0 (`scripts/`); der Code liegt wieder in einer Datei.
- `requirements.txt` mit Paketliste und jede `pyproject.toml`-Konfiguration.
- Vier wirkungslose Einstellungen aus 1.3/2.0: `separator_length`,
  `output_width`, `max_displayed_multi_games`, `max_games_per_language`.
  Die Breite kommt jetzt vom Terminal, die Detailausgabe ist vollständig.

### Behoben

- `sort --dry-run` kopiert nicht mehr. 2.0 setzte den Status auf DRY_RUN,
  schrieb aber trotzdem alle Dateien nach `workdir/sort`.
- `cleanup --dry-run` und `run --dry-run` löschen nichts mehr, sondern zeigen
  nur, welche Ordner betroffen wären. 2.0 löschte auch im Trockenlauf.
- Ein lokaler Download, der größer ist als das Remote-Original, wird neu
  geladen statt angehängt. 1.3/2.0 öffneten jede vorhandene Datei im
  Append-Modus und zerstörten damit die Datei.
- Docstring in 2.0 (`ftp_client.py`) enthielt versehentlich fremde Schriftzeichen.
- Sprach-Fallback bei Spielen ohne `De`/`En`/`Fr` entschied nach sortierter
  statt nach Dateisystem-Reihenfolge. Bei `Hugo2 v1.1` (nur `Dk` und `Fi`)
  behielt 2.5 dadurch `Dk`, 1.3 dagegen `Fi`. Betroffen war genau dieses eine
  Spiel von 3533.

### Geprüft

- `python3 -m py_compile` ohne Fehler.
- `--help` für Hauptbefehl und alle Subcommands ohne Traceback.
- Gleichheit zu 1.3 und 2.0: Parser-Ergebnisse für 3533 echte Dateinamen,
  Sort- und Finalize-Bäume auf einem 26-Archiv-Korpus (Pfade, Größen, mtimes)
  sowie 205 Archive aus dem echten 2.0-Bestand. Keine Abweichung.
- Vollständiger Lauf auf dem echten Bestand: 3534 Archive (Vereinigung der
  beiden vorhandenen Ergebnisbäume) durch 1.3 und 2.5 finalisiert, je 3533
  Dateien, identische Pfade, Größen und mtimes.
- Layout-Prüfung: Panels, Tabellen und Balken schließen bei 60, 80 und 120
  Spalten exakt, auch mit Emoji und Umlauten.

## [2.0.0]

Modulare Fassung mit `rich`, `typer`, `loguru` und `pytest`; `workdir/`-Trennung,
Fortschrittsbalken in KB, Status-Icons und Abschlusstabelle.

## [1.3.0]

Monolithisches Skript mit der Standardbibliothek, globaler Konfiguration und
den Ordnern `whdload-mirror/`, `whdload-sort/`, `whdload-final/` neben dem Skript.
