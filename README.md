# WHDLoad Workflow 2.5

**Kompletter Workflow für WHDLoad-Archive: Mirror, Sort, Compare, Finalize und Cleanup**

Ein terminalbasiertes Tool für Amiga-Enthusiasten, das den Lebenszyklus einer
WHDLoad-Sammlung automatisiert: Download vom Retroplay-FTP-Server, Sortierung nach
Amiga-System, Sprachauswahl pro Spiel und Aufräumen der Zwischenstände.

2.5 ist die Zusammenführung von 1.3 (ein Skript, keine Abhängigkeiten) und 2.0
(modular, Rich-Oberfläche). Ergebnis: **die Optik von 2.0 in einer einzigen Datei
ohne ein einziges `pip`-Paket.**

---

## Voraussetzungen

- Python **3.10 oder neuer**
- Sonst nichts. Kein `rich`, kein `typer`, kein `loguru`, kein `pytest`.
- Ein FTP-Zugang ist nicht nötig: der Retroplay-Server erlaubt anonymen Zugang.

```bash
python3 --version      # muss 3.10+ sein
```

Es gibt bewusst keine virtuellen Umgebungen, kein `pip install`, keine
`requirements.txt` und keine `pyproject.toml`: es wird schlicht nichts
installiert.

---

## Schnellstart

```bash
# im Projektverzeichnis
python3 whdload_workflow.py                    # Hilfe
python3 whdload_workflow.py --version          # Version
python3 whdload_workflow.py info               # aktuelle Konfiguration
python3 whdload_workflow.py run                # alle fünf Schritte, mit Rückfragen
python3 whdload_workflow.py run --step 2       # nur Mirror und Sort
python3 whdload_workflow.py sort --dry-run     # Sortierung nur analysieren
```

---

## Die fünf Schritte

| Schritt | Befehl | Was passiert | Ziel |
|---|---|---|---|
| 1 | `mirror` | lädt `*.lha` rekursiv vom FTP-Server | `workdir/mirror/` |
| 2 | `sort` | verteilt nach AGA / CD32 / CDTV / Rest | `workdir/sort/` |
| 3 | `compare` | vergleicht zwei Verzeichnisse auf Dateinamen | keine Dateiaktion |
| 4 | `finalize` | wählt je Spiel die beste Sprachversion | `workdir/final/` |
| 5 | `cleanup` | löscht mirror und sort | gibt Platz frei |

Sortierregel (Reihenfolge ist entscheidend, das erste Treffer-Kriterium gewinnt):

```
"AGA"  im Dateinamen  -> amiga1200/
"CD32"                -> amigacd32/
"CDTV"                -> amigacdtv/
sonst                 -> amiga500/   (alphabetische Unterordner A/, B/, ... bleiben erhalten)
```

Sprachauswahl in `finalize`: Priorität `De > En > Fr`. Die erste Sprache mit
mindestens einem Treffer gewinnt, und **alle** Dateien dieser Sprache bleiben
erhalten. Ein Spiel mit einer deutschen Version verliert also seine englischen
Varianten, behält aber mehrere deutsche Versionsstände. Gibt es keine der
priorisierten Sprachen, bleibt die erste gefundene Sprachgruppe.

---

## CLI-Referenz

Alle Befehle akzeptieren `--no-color` (wirkt auch über die Umgebungsvariable
`NO_COLOR`). Farben werden automatisch abgeschaltet, wenn die Ausgabe in eine
Datei oder Pipe geht.

### `run` - kompletter Workflow

| Option | Wirkung |
|---|---|
| `-n`, `--dry-run` | Simulation: kein Download, kein Kopieren, kein Löschen |
| `--no-confirm` | keine Rückfragen (für Skripte und CI) |
| `-s N`, `--step N` | nur die Schritte 1 bis N (1-5) |
| `-v`, `--verbose` | DEBUG-Ausgaben im Terminal, Stacktraces |
| `--ftp-host HOST` | FTP-Server überschreiben |
| `--remote-path PFAD` | Remote-Startpfad überschreiben |

### Einzelschritte

| Befehl | Optionen |
|---|---|
| `mirror` | `-n/--dry-run`, `--ftp-host`, `--remote-path`, `-v` |
| `sort` | `-s/--source`, `-t/--target`, `-n/--dry-run`, `-v` |
| `compare` | `--dir1`, `--dir2`, `-v` |
| `finalize` | `-n/--dry-run`, `-s/--source`, `-t/--target`, `-v` |
| `cleanup` | `-f/--force`, `-n/--dry-run`, `-v` |
| `info` | `-v` |

Rückfragen erwarten `j` oder `n` (auch `ja`, `nein`, `y`, `yes`, `no`). Der
Standardwert steht in Klammern: `[J/n]` bedeutet "Enter" ist Ja.

**Wichtig bei nicht-interaktiver Eingabe:** Lässt sich keine Eingabe lesen
(Pipe, CI), beantwortet das Programm jede Rückfrage mit **Nein**. Ein
unbeaufsichtigter Lauf löscht also nichts und kopiert nichts, was nicht
ausdrücklich per `--no-confirm` freigegeben wurde.

### Exit-Codes

| Code | Bedeutung |
|---|---|
| 0 | alles erledigt, kein Schritt im Zustand ERROR |
| 1 | mindestens ein Schritt ist fehlgeschlagen |
| 2 | Aufruffehler (unbekannte Option, ungültiger Wert) |
| 130 | Abbruch durch Strg+C |

---

## workdir

Alle Laufzeitdaten liegen unterhalb von `./workdir`, relativ zum aktuellen
Arbeitsverzeichnis. Das Skript selbst schreibt nichts daneben.

```
WHDLoad_Workflow/           Repository-Wurzel
├── whdload_workflow.py     die komplette Anwendung
├── README.md               diese Datei
├── CHANGELOG.md            Versionshistorie
├── LICENSE                 MIT
└── workdir/                wird automatisch angelegt
    ├── mirror/             FTP-Download-Cache      (temporär, Cleanup löscht)
    ├── sort/               sortierte Archive       (temporär, Cleanup löscht)
    ├── final/              fertige Archive         (bleibt)
    └── logs/               Protokolle              (bleibt)
```

`AGENTS.md`, `blueprint/`, `.agents/` und `.codebase-memory/` liegen lokal
daneben und sind per `.gitignore` ausgeschlossen; sie gehören nicht ins
Repository.

| Inhalt | Pfad | Lebensdauer |
|---|---|---|
| Fertige, deduplizierte Archive | `workdir/final/` | bleibt |
| Log-Dateien | `workdir/logs/` | bleibt |
| FTP-Cache | `workdir/mirror/` | Cleanup löscht |
| Zwischenstand der Sortierung | `workdir/sort/` | Cleanup löscht |

`workdir/` steht in der `.gitignore` des Repositories. Nach einem kompletten
Lauf liegen dort mehrere Gigabyte; die gehören nicht ins Repository.

---

## Konfiguration

Alle Voreinstellungen stehen als Dataclasses oben in `whdload_workflow.py`
(Block 1) und lassen sich dort ändern. Ein Konfigurationsfile gibt es bewusst
nicht: eine Datei, die gelesen werden muss, ist eine Fehlerquelle mehr.

| Bereich | Feld | Standard |
|---|---|---|
| FTP | `host` | `ftp2.grandis.nu` |
| FTP | `port` / `user` / `password` | `21` / `ftp` / `any` |
| FTP | `remote_path` | `/Retroplay WHDLoad Packs/Commodore_Amiga_-_WHDLoad_-_Games/` |
| FTP | `use_passive` | `True` |
| FTP | `timeout` | `300` s |
| FTP | `max_retries` / `sleep_between_retries` | `3` / `2.0` s |
| FTP | `verify_hash` | `auto` (Serverhash, sonst Größe) |
| Anzeige | `path_rewrite_from` / `path_rewrite_to` | kürzt den langen FTP-Basispfad in der Ausgabe |
| Sort | `source_dir` / `target_dir` | `workdir/mirror` / `workdir/sort` |
| Compare | `default_dir1` / `default_dir2` | `workdir/mirror` / `workdir/sort` |
| Finalize | `source_dir` / `target_dir` | `workdir/sort` / `workdir/final` |
| Finalize | `language_priority` | `["De", "En", "Fr"]` |
| Finalize | `underscores_to_spaces` | `True` |
| Allgemein | `log_dir` | `workdir/logs` |
| Allgemein | `archive_extension` | `.lha` |

---

## Logging

Jeder Lauf schreibt eine eigene Datei nach `workdir/logs/`:

```
workdir/logs/2026-10-31_00-16-56-whdload_workflow.log
```

Aufbau der Zeilen, identisch zu 2.0:

```
2026-10-31 00:16:56 | INFO     | Logging initialisiert - Log-Datei: workdir/logs/...
2026-10-31 00:16:56 | INFO     | [UI] ╭─ WHDLoad Workflow v2.5.0 ──────────────╮
```

Jede Terminalausgabe wird als `[UI]`-Zeile gespiegelt, aber **ohne**
ANSI-Sequenzen: die Logdatei bleibt grep- und diff-fähig.

Im Terminal erscheinen nur Warnungen und Fehler. Mit `--verbose` kommt alles
ab DEBUG dazu, inklusive Stacktraces.

---

## Sicherheit und Ergebnisgarantie

Das Programm ist bewusst defensiv gebaut, weil ein falsch konfigurierter Pfad
hier über mehrere Gigabyte entscheidet:

- **Quellarchive werden nie verändert.** `mirror` schreibt nur neue Dateien,
  `sort` und `finalize` kopieren ausschließlich (`shutil.copy2`, inklusive
  Zeitstempel).
- **Cleanup ist eingezäunt.** Gelöscht wird nur, was aufgelöst innerhalb von
  `./workdir` liegt. `workdir` selbst, `workdir/final` und `workdir/logs` sind
  gesperrt. Ein Eintrag wie `..` oder ein Symlink nach außen wird abgelehnt,
  nicht ausgeführt.
- **Quelle gleich Ziel wird abgelehnt.** Sort und Finalize brechen ab, wenn
  Quell- und Zielordner identisch sind oder das Ziel innerhalb der Quelle
  liegt. Sonst würde ein zweiter Lauf seine eigenen Ergebnisse mitkopieren.
- **Resume hängt nur an, wenn es fortsetzt.** Eine lokale Datei, die größer ist
  als das Original, wird neu geladen statt angehängt.
- **Fehler sind isoliert.** Ein kaputter Download beendet nicht den restlichen
  Lauf; er wird gezählt und der Schritt endet mit Status ERROR.
- **Reihenfolge wie in 1.3/2.0.** Dateien werden in der Reihenfolge verarbeitet,
  in der Server und Dateisystem sie liefern. Das ist kein Zufall: bei Spielen
  ohne deutsche, englische oder französische Version entscheidet die
  Fundreihenfolge, welche Sprache gewinnt (siehe Hugo2 unten). Ein Umsortieren
  würde dort ein anderes Archiv behalten.

### Der Sprach-Fallback ist reihenfolgeabhängig

Gibt es zu einem Spiel weder `De` noch `En` noch `Fr`, behält Finalize die
**erste gefundene** Sprachgruppe. Bei `Hugo2 v1.1` existieren nur `Dk` und `Fi`.
Welche der beiden Dateien gewinnt, hängt davon ab, welche das Dateisystem zuerst
liefert. 1.3, 2.0 und 2.5 verhalten sich hier gleich, weil sie dieselbe
Reihenfolge benutzen. Wer das Verhalten ändern will, muss `language_priority`
erweitern (z. B. `["De", "En", "Fr", "It", "Dk", "Fi"]`) und dann bewusst neu
finalisieren.

### Nachgewiesene Gleichheit zu 1.3 und 2.0

Für die Abnahme wurden 1.3, 2.0 und 2.5 auf denselben Daten ausgeführt und
verglichen (Pfade, Dateigrößen, mtimes):

| Prüfung | Umfang | Ergebnis |
|---|---|---|
| `parse_sort` / `parse_finalize` | alle 3533 echten Dateinamen aus dem 2.0-Lauf | identisch in allen drei Versionen |
| Sort-Baum | 26 Archive mit Mehrsprachigkeit, AGA/CD32/CDTV, ohne Version, Unterordner | identisch |
| Finalize-Baum | dieselben 26 Archive, inklusive Umbenennung und Deduplikation | identisch |
| Echter Datenbestand | 205 Archive aus `workdir/final`, zurückgebaut und erneut finalisiert | identisch (Pfade und Größen) |
| Gesamter Bestand | 3534 Archive, je 3533 Ergebnisdateien aus 1.3 und 2.5 | identisch (Pfade, Größen, mtimes) |

Ergebnis: 0 Abweichungen.

---

## Unterschiede zu 1.3 und 2.0

**Gegenüber 1.3**

- Daten liegen in `workdir/` statt in `whdload-mirror/`, `whdload-sort/`,
  `whdload-final/` neben dem Skript.
- Farbige Oberfläche mit Panels, Tabellen und Fortschrittsbalken.
- Subcommands statt rein interaktivem Ablauf; `--dry-run`, `--no-confirm`,
  `--step`.
- Statistiken als Dataclasses statt globaler Variablen.

**Gegenüber 2.0**

- Keine Abhängigkeiten: `rich`, `typer`, `loguru` und `pytest` entfallen.
- Ein Skript statt 18 Module.
- `pytest`-Tests entfallen; die Abnahme erfolgt über `py_compile`, `--help` und
  den Gleichheitsvergleich oben.

**Bewusste Korrekturen (kein Einfluss auf korrekte Daten)**

1. `--dry-run` ist ein echter Trockenlauf: `sort` kopiert nicht, `cleanup`
   löscht nicht, `mirror` verbindet sich nicht. 2.0 meldete bei `sort`
   DRY_RUN und schrieb trotzdem, und sein Cleanup löschte auch im Dry-Run.
2. Ein lokaler Download, der größer als das Original ist, wird neu geladen
   statt angehängt. 1.3 und 2.0 hingen in diesem Fall an eine defekte Datei an.
3. Vier wirkungslose Einstellungen wurden entfernt (`separator_length`,
   `output_width`, `max_displayed_multi_games`, `max_games_per_language`).
   Die Breite ergibt sich jetzt aus dem Terminal, die Detailausgabe ist
   vollständig.

---

## Lizenz

MIT, siehe [LICENSE](LICENSE).
