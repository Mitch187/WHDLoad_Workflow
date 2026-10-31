#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
================================================================================
WHDLoad Workflow 2.5
Kompletter Workflow fuer WHDLoad-Archive: Mirror, Sort, Compare, Finalize, Cleanup
================================================================================

Version: 2.5.0
Lizenz:  MIT (siehe LICENSE)

WAS DIESES PROGRAMM TUT
-----------------------
Es automatisiert den kompletten Lebenszyklus einer WHDLoad-Sammlung:

  1. FTP Mirror  laedt die Archive (*.lha) rekursiv vom Retroplay-Server
  2. Sort        verteilt sie nach Amiga-System (500 / 1200 / CD32 / CDTV)
  3. Compare     vergleicht zwei Verzeichnisse auf Dateinamen-Ebene
  4. Finalize    waehlt pro Spiel die beste Sprachversion und kopiert sie
  5. Cleanup     loescht die temporaeren Zwischenordner

WARUM ES DIESE VERSION GIBT
---------------------------
1.3 war ein Monolith ohne Abhaengigkeiten, 2.0 war modular, aber brauchte
rich, typer, loguru und pytest. 2.5 vereint beides: die Fachlogik und die
Determinismus-Eigenschaften von 1.3, die Terminal-Optik und die
workdir-Kapselung von 2.0, und zwar ausschliesslich mit der
Python-Standardbibliothek. Es gibt nichts zu installieren:

    python3 whdload_workflow.py run

AUFBAU DER DATEI
----------------
Die Datei ist bewusst ein Monolith (wie 1.3) und in sechs Bloecke gegliedert,
die man von oben nach unten lesen kann:

  1. KONFIGURATION & KONSTANTEN   Dataclasses, Pfade, Regexe, Datenmodelle
  2. TERMINAL UI & LOGGING ENGINE ANSI-Farben, Rahmen, Tabellen, Fortschritt
  3. WORKDIR & DATEI-HANDLING     Pfade, Groessen, Hashing, Dateinamen-Parser
  4. WHDLOAD KERNLOGIK            FTP-Client: Listing, Resume, Verifikation
  5. DIE FUENF SCHRITTE           mirror, sort, compare, finalize, cleanup
  6. CLI & MAIN                   argparse, Orchestrierung, Summary

SICHERHEITSVERSPRECHEN
----------------------
Das Programm schreibt ausschliesslich unterhalb von ./workdir. Quellarchive
werden nur gelesen, niemals verschoben oder geloescht. Cleanup loescht nur
workdir/mirror und workdir/sort und verweigert alles ausserhalb des workdir.
================================================================================
"""

# ============================================================================
# === 1. KONFIGURATION & KONSTANTEN ==========================================
# ============================================================================
#
# Alles, was sich ohne Eingriff in die Logik aendern laesst, steht hier oben.
# Die Werte sind identisch mit 1.3 und 2.0 - wer hier etwas aendert, aendert
# das Verhalten aller drei Versionen gleichermassen.

import argparse
import ftplib
import hashlib
import logging
import os
import re
import shutil
import socket
import sys
import threading
import time
import unicodedata
import zlib
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from posixpath import join as pjoin, normpath

PROGRAMM = "WHDLoad Workflow"
VERSION = "2.5.0"

# --- workdir-Struktur -------------------------------------------------------
# 2.0 hat Code und Daten getrennt: alles Laufzeitliche liegt in workdir/.
# Das ist der wichtigste Unterschied zu 1.3, wo mirror/sort/final als
# Geschwisterordner des Skripts lagen. Die Kapselung macht .gitignore
# trivial und verhindert, dass Arbeitsdaten im Repository landen.
WORKDIR = "workdir"
MIRROR_DIR = os.path.join(WORKDIR, "mirror")
SORT_DIR = os.path.join(WORKDIR, "sort")
FINAL_DIR = os.path.join(WORKDIR, "final")
LOG_DIR = os.path.join(WORKDIR, "logs")


@dataclass
class FTPConfig:
    """Server- und Transferparameter fuer Schritt 1."""

    host: str = "ftp2.grandis.nu"
    port: int = 21
    user: str = "ftp"
    password: str = "any"
    remote_path: str = "/Retroplay WHDLoad Packs/Commodore_Amiga_-_WHDLoad_-_Games/"
    use_passive: bool = True
    # 300 s: grosse Archive brauchen auch bei langsamer Leitung lange, und
    # ein zu knapper Timeout bricht mitten im Transfer ab.
    timeout: int = 300
    max_retries: int = 3
    # Wird mit der Versuchsnummer multipliziert (linearer Backoff).
    sleep_between_retries: float = 2.0
    # 1 MiB: Kompromiss aus Uebertragungseffizienz und Reaktionsfaehigkeit
    # auf Abbrueche (bei kleineren Bloecken geht weniger bei Abbruch verloren).
    blocksize: int = 1024 * 1024
    resume_partial: bool = True
    skip_existing_same_size: bool = True
    set_mtime: bool = True
    # "auto" heisst: Server-Hash wenn verfuegbar, sonst Groessenvergleich.
    verify_hash: str = "auto"
    # Reihenfolge = Prioritaet. CRC32 zuerst, weil es auf dem Server am
    # haeufigsten angeboten wird und lokal am guenstigsten zu berechnen ist.
    preferred_hash_algos: list[str] = field(
        default_factory=lambda: ["CRC32", "MD5", "SHA-1", "SHA-256"]
    )


@dataclass
class DisplayConfig:
    """
    Nur Anzeige, niemals echte Pfade.

    Der Retroplay-Basispfad ist so lang, dass jede Logzeile umbricht. Er wird
    fuer die Ausgabe durch einen Alias ersetzt; die FTP-Kommunikation nutzt
    immer den Originalpfad.
    """

    path_rewrite_enabled: bool = True
    path_rewrite_from: str = (
        "/Retroplay WHDLoad Packs/Commodore_Amiga_-_WHDLoad_-_Games/"
    )
    path_rewrite_to: str = "/Retroplay WHDLoad.../... "
    path_maxlen: int = 0
    path_ellipsis: str = "middle"


@dataclass
class SortConfig:
    """Quell-/Zielordner und Namen der System-Unterordner fuer Schritt 2."""

    source_dir: str = MIRROR_DIR
    target_dir: str = SORT_DIR
    folder_amiga1200: str = "amiga1200"
    folder_amigacd32: str = "amigacd32"
    folder_amigacdtv: str = "amigacdtv"
    folder_amiga500: str = "amiga500"


@dataclass
class CompareConfig:
    """Default-Verzeichnisse fuer Schritt 3."""

    default_dir1: str = MIRROR_DIR
    default_dir2: str = SORT_DIR


@dataclass
class FinalizeConfig:
    """
    Sprachauswahl und Deduplikation fuer Schritt 4.

    language_priority ist eine echte Prioritaetsliste: die erste Sprache mit
    mindestens einem Treffer gewinnt, und zwar komplett. Ein Spiel mit
    De-Version behaelt also alle De-Versionen und verliert alle En-Versionen.
    """

    source_dir: str = SORT_DIR
    target_dir: str = FINAL_DIR
    platform_folders: list[str] = field(
        default_factory=lambda: ["amiga1200", "amigacd32", "amigacdtv", "amiga500"]
    )
    default_language: str = "En"
    language_priority: list[str] = field(default_factory=lambda: ["De", "En", "Fr"])
    underscores_to_spaces: bool = True


@dataclass
class CleanupConfig:
    """
    Was Cleanup loeschen darf.

    workdir/final und workdir/logs stehen bewusst NICHT in dieser Liste: das
    Ergebnis und das Protokoll eines Laufs sind das Wertvollste und duerfen
    von einem Aufraeumschritt nicht verschwinden.
    """

    folders_to_delete: list[tuple[str, str]] = field(
        default_factory=lambda: [
            (MIRROR_DIR, "workdir/mirror (FTP-Download-Cache)"),
            (SORT_DIR, "workdir/sort (sortierte Archive)"),
        ]
    )


@dataclass
class GeneralConfig:
    """Querschnittseinstellungen inklusive der Parser-Regexe."""

    log_dir: str = LOG_DIR
    log_encoding: str = "utf-8"
    archive_extension: str = ".lha"
    # Der vierstellige Code am Ende (z.B. _2024 oder _2023&2024) ist reine
    # Sortierhilfe des FTP-Servers und gehoert nicht zum Spielnamen.
    zahlencode_pattern: str = r"_(\d{4}(?:&\d{4})*)$"
    # Sprachcodes sind Gross-Klein-Paare: De, En, Fr, DeEn, DeEnFr.
    # Das Muster verhindert, dass "AGA" oder "HiRes" als Sprache gilt.
    sprachcode_pattern: str = r"^[A-Z][a-z](?:[A-Z][a-z])*$"


@dataclass
class Config:
    """
    Top-Level-Konfiguration.

    Die Teilkonfigurationen sind absichtlich getrennt: CLI-Overrides betreffen
    immer genau einen Bereich, und die Schritte lesen nur ihren eigenen Teil.
    """

    ftp: FTPConfig = field(default_factory=FTPConfig)
    display: DisplayConfig = field(default_factory=DisplayConfig)
    sort: SortConfig = field(default_factory=SortConfig)
    compare: CompareConfig = field(default_factory=CompareConfig)
    finalize: FinalizeConfig = field(default_factory=FinalizeConfig)
    cleanup: CleanupConfig = field(default_factory=CleanupConfig)
    general: GeneralConfig = field(default_factory=GeneralConfig)

    @property
    def mirror_folder(self) -> str:
        """Der FTP-Zielordner ist zugleich die Quelle des Sort-Schritts."""
        return self.sort.source_dir


# --- Datenmodelle fuer Ergebnisse und Statistiken ---------------------------
# 1.3 sammelte diese Werte in globalen Variablen. Das ist bei nur einem Lauf
# korrekt, macht die Funktionen aber unbrauchbar fuer Tests und fuer einen
# zweiten Lauf im selben Prozess. 2.5 nutzt deshalb Dataclasses.


class StepStatus:
    """Status eines Workflow-Schritts (bewusst als Konstanten, kein Enum)."""

    OK = "OK"
    SKIPPED = "SKIPPED"
    ERROR = "ERROR"
    DRY_RUN = "DRY_RUN"


@dataclass
class StepResult:
    """Ergebnis eines Schritts fuer die Abschlusstabelle."""

    number: int
    name: str
    status: str
    duration_sec: float = 0.0
    files_processed: int = 0
    detail: str = ""
    errors: list[str] = field(default_factory=list)

    @property
    def status_icon(self) -> str:
        """Farbiges Statuszeichen fuer die Summary-Tabelle."""
        return {
            StepStatus.OK: farbe("✓", HELLGRUEN),
            StepStatus.SKIPPED: farbe("→", GELB),
            StepStatus.ERROR: farbe("✗", ROT),
            StepStatus.DRY_RUN: farbe("○", CYAN),
        }.get(self.status, "?")

    @property
    def duration_str(self) -> str:
        """Dauer in der Einheit, die fuer den Wert am lesbarsten ist."""
        return formatiere_zeitdauer(self.duration_sec, kurz=True)


@dataclass
class DownloadStats:
    """Statistik genau eines Datei-Downloads."""

    action: str = "downloaded"
    bytes_downloaded: int = 0
    secs: float = 0.0
    mbps: float = 0.0
    verify: str = "SKIPPED"


@dataclass
class MirrorStats:
    """Summierte Statistik des Mirror-Schritts."""

    folders_synced: int = 0
    files_synced: int = 0
    bytes_downloaded: int = 0
    transfer_seconds: float = 0.0
    files_skipped: int = 0
    files_resumed: int = 0
    files_error: int = 0
    hash_supported: bool = False
    hash_algo: str | None = None
    xcrc_supported: bool = False
    errors: list[str] = field(default_factory=list)

    @property
    def avg_mbps(self) -> float:
        """Durchschnittsgeschwindigkeit bezogen auf die reine Transferzeit."""
        if self.transfer_seconds > 0:
            return (self.bytes_downloaded / (1024 * 1024)) / self.transfer_seconds
        return 0.0


@dataclass
class SortStats:
    """Verteilung der Archive auf die vier Systeme plus Randstatistiken."""

    total_files: int = 0
    amiga1200: int = 0
    amigacd32: int = 0
    amigacdtv: int = 0
    amiga500: int = 0
    errors: int = 0
    additional_info_counts: dict[str, int] = field(default_factory=dict)
    language_counts: dict[str, int] = field(default_factory=dict)


@dataclass
class CompareStats:
    """Mengen-Differenz zweier Verzeichnisse."""

    files_dir1: int = 0
    files_dir2: int = 0
    common: int = 0
    only_in_dir1: list[str] = field(default_factory=list)
    only_in_dir2: list[str] = field(default_factory=list)
    paths_dir1: dict[str, list[str]] = field(default_factory=dict)
    paths_dir2: dict[str, list[str]] = field(default_factory=dict)


@dataclass
class FinalizePlatformResult:
    """Ergebnis eines Plattform-Unterordners in Schritt 4."""

    subdir: str = ""
    total: int = 0
    selected: list[dict] = field(default_factory=list)
    removed: list[dict] = field(default_factory=list)
    games: dict[str, list[dict]] = field(default_factory=dict)
    lang_stats: dict[str, int] = field(default_factory=dict)
    games_by_lang: dict[str, list[str]] = field(default_factory=dict)
    files_copied: int = 0
    copy_errors: list[str] = field(default_factory=list)


@dataclass
class FinalizeStats:
    """Aggregat ueber alle Plattformen in Schritt 4."""

    platforms: list[FinalizePlatformResult] = field(default_factory=list)

    @property
    def total_files(self) -> int:
        return sum(p.total for p in self.platforms)

    @property
    def total_selected(self) -> int:
        return sum(len(p.selected) for p in self.platforms)

    @property
    def total_removed(self) -> int:
        return sum(len(p.removed) for p in self.platforms)

    @property
    def total_copied(self) -> int:
        return sum(p.files_copied for p in self.platforms)

    @property
    def savings_percent(self) -> float:
        if self.total_files > 0:
            return (self.total_removed / self.total_files) * 100
        return 0.0


@dataclass
class CleanupStats:
    """Was Cleanup geloescht, behalten oder uebersprungen hat."""

    deleted: list[str] = field(default_factory=list)
    kept: list[str] = field(default_factory=list)
    skipped: list[str] = field(default_factory=list)
    bytes_freed: int = 0

    @property
    def gb_freed(self) -> float:
        return self.bytes_freed / (1024**3)


class WHDLoadError(Exception):
    """Erwarteter Fachfehler; die CLI zeigt ihn ohne Stacktrace im Terminal."""


class FTPConnectionError(WHDLoadError):
    """Verbindungsaufbau oder Verbindungshaltung endgueltig gescheitert."""


# === 2. TERMINAL UI & LOGGING ENGINE ========================================
# ============================================================================
#
# 2.0 hat die Optik mit Rich erzeugt. Rich ist nicht verfuegbar, also wird das
# Notwendige nachgebaut: Farben, Rahmen, Tabellen, Fortschrittsbalken. Der
# Umfang ist bewusst klein und auf die tatsaechlich benutzten Formen
# beschraenkt - ein halbes Rich waere teurer zu warten als diese 300 Zeilen.
#
# Grundregel: Jede Breitenberechnung laeuft ueber sichtbare_breite(), weil
# ANSI-Sequenzen keine Spalten belegen, Emoji aber zwei. Wer stattdessen
# len() benutzt, bekommt schiefe Rahmen.

# 256-Farb-Panel: die Indizes sind so gewaehlt, dass sie den Rich-Farben
# (cyan, blue, magenta, green, yellow, red) optisch entsprechen.
SCHWARZ = 16
ROT = 196
HELLGRUEN = 46
GRUEN = 35
GELB = 220
BLAU = 33
CYAN = 51
MAGENTA = 201
WEISS = 255
GRAU = 245
DUNKELGRAU = 240

# 24-Bit-Werte fuer Terminals mit COLORTERM=truecolor. Sie liegen absichtlich
# nahe an Richs "standard"-Palette, damit die Optik vertraut wirkt.
RGB = {
    ROT: (255, 85, 85),
    HELLGRUEN: (95, 215, 95),
    GRUEN: (0, 175, 95),
    GELB: (255, 215, 95),
    BLAU: (95, 135, 255),
    CYAN: (0, 215, 215),
    MAGENTA: (255, 95, 215),
    WEISS: (238, 238, 238),
    GRAU: (160, 160, 160),
    DUNKELGRAU: (108, 108, 108),
    SCHWARZ: (0, 0, 0),
}

# Wird von der CLI beim Start gesetzt (--no-color, NO_COLOR, Pipe-Erkennung).
FARBEN_AKTIV = True
_TRUE_COLOR = False

RESET = "\x1b[0m"
BOLD = "\x1b[1m"
DIM = "\x1b[2m"
KURSIV = "\x1b[3m"

_ANSI_MUSTER = re.compile(r"\x1b\[[0-9;]*m")

# Spinner- und Balkenzeichen. Die Braille-Folge ist die uebliche, optisch
# ruhigste Variante fuer "es passiert etwas, Dauer unbekannt".
SPINNER = ["⠋", "⠙", "⠹", "⠸", "⠼", "⠴", "⠦", "⠧", "⠇", "⠏"]
BALKEN_VOLL = "━"
BALKEN_LEER = "─"


def farbmodus_setzen(aktiv: bool, true_color: bool = False) -> None:
    """Schaltet Farben global aus/ein (CLI-Flag, NO_COLOR, Pipe)."""
    global FARBEN_AKTIV, _TRUE_COLOR
    FARBEN_AKTIV = aktiv
    _TRUE_COLOR = true_color


def farbe(text: str, farbcode: int, fett: bool = False, dunkel: bool = False) -> str:
    """
    Faerbt Text ein, ohne die Breite zu veraendern.

    Ohne Farbmodus wird der Text unveraendert zurueckgegeben; alle Aufrufer
    koennen deshalb so tun, als gaebe es immer Farbe.
    """
    if not FARBEN_AKTIV:
        return text
    praefix = ""
    if fett:
        praefix += BOLD
    if dunkel:
        praefix += DIM
    if _TRUE_COLOR and farbcode in RGB:
        r, g, b = RGB[farbcode]
        praefix += f"\x1b[38;2;{r};{g};{b}m"
    else:
        praefix += f"\x1b[38;5;{farbcode}m"
    return f"{praefix}{text}{RESET}"


def sichtbare_breite(text: str) -> int:
    """
    Breite eines Strings in Terminalzellen.

    Drei Faelle, die len() falsch beantwortet:
      - ANSI-Sequenzen belegen keine Zelle
      - ostasiatische Zeichen und Emoji belegen zwei
      - kombinierende Zeichen belegen keine
    """
    ohne_ansi = _ANSI_MUSTER.sub("", text)
    breite = 0
    for zeichen in ohne_ansi:
        if unicodedata.combining(zeichen):
            continue
        if unicodedata.east_asian_width(zeichen) in ("W", "F"):
            breite += 2
        elif 0x1F300 <= ord(zeichen) <= 0x1FAFF:
            # Emoji-Range: east_asian_width meldet hier nicht zuverlaessig "W".
            breite += 2
        elif zeichen in ("\uFE0F", "\u200D"):
            continue
        else:
            breite += 1
    return breite


def kuerze_auf_breite(text: str, breite: int) -> str:
    """Kuerzt sichtbaren Text auf `breite` Zellen und markiert die Kuerzung."""
    if breite <= 0:
        return ""
    if sichtbare_breite(text) <= breite:
        return text
    if breite == 1:
        return "…"
    ziel = breite - 1
    ergebnis = ""
    belegt = 0
    for zeichen in _ANSI_MUSTER.sub("", text):
        z = 2 if (
            unicodedata.east_asian_width(zeichen) in ("W", "F")
            or 0x1F300 <= ord(zeichen) <= 0x1FAFF
        ) else 1
        if belegt + z > ziel:
            break
        ergebnis += zeichen
        belegt += z
    return ergebnis + "…"


def _pad(text: str, breite: int, ausrichtung: str = "links") -> str:
    """Fuellt Text auf eine feste Zellbreite auf (ANSI-sicher)."""
    fehlend = breite - sichtbare_breite(text)
    if fehlend <= 0:
        return text
    if ausrichtung == "rechts":
        return " " * fehlend + text
    if ausrichtung == "mitte":
        links = fehlend // 2
        return " " * links + text + " " * (fehlend - links)
    return text + " " * fehlend


def terminal_breite() -> int:
    """
    Breite des Terminals, mit sicherem Fallback.

    In Pipes meldet shutil oft 0 oder 80; 0 wuerde jeden Rahmen zerreissen,
    deshalb die Untergrenze. Die Obergrenze verhindert, dass Panels auf
    Ultrawide-Monitoren unlesbar auseinanderlaufen.
    """
    try:
        breite = shutil.get_terminal_size(fallback=(100, 30)).columns
    except Exception:
        breite = 100
    return max(40, min(breite, 140))


def _zeilenumbruch(text: str, breite: int) -> list[str]:
    """
    Bricht Text an Wortgrenzen; lange Woerter werden hart getrennt.

    Passt die Zeile bereits, wird sie unveraendert zurueckgegeben. Das ist
    keine Optimierung, sondern noetig: ASCII-Art und eingerueckte
    Aufzaehlungen bestehen aus mehreren Leerzeichen, die ein
    split()/join() zerstören wuerde.
    """
    if breite <= 0 or sichtbare_breite(text) <= breite:
        return [text]
    woerter = text.split()
    zeilen: list[str] = []
    aktuell = ""
    for wort in woerter:
        kandidat = f"{aktuell} {wort}".strip()
        if sichtbare_breite(kandidat) <= breite:
            aktuell = kandidat
            continue
        if aktuell:
            zeilen.append(aktuell)
        while sichtbare_breite(wort) > breite:
            teil = kuerze_auf_breite(wort, breite)
            zeilen.append(teil)
            wort = wort[len(teil) - 1 :]
        aktuell = wort
    if aktuell:
        zeilen.append(aktuell)
    return zeilen or [""]


# --- Logging ---------------------------------------------------------------
# 2.0 nutzte Loguru mit zwei Sinks: Datei (DEBUG, alles) und Terminal
# (WARNING, bei --verbose DEBUG). Genau dieses Verhalten entsteht hier mit
# dem Standardmodul. Der Datei-Formatter erzeugt dieselbe Zeilenform wie
# 2.0, damit vorhandene Log-Auswertungen weiter funktionieren:
#     2026-10-30 20:54:46 | INFO     | [UI] ...
LOG_FORMAT_DATEI = "%(asctime)s | %(levelname)-8s | %(message)s"
LOG_DATUM = "%Y-%m-%d %H:%M:%S"

logger = logging.getLogger("whdload")


class _FarbigesLevelFormatter(logging.Formatter):
    """Terminal-Formatter: farbiges Level, cyanfarbene Nachricht."""

    def format(self, record: logging.LogRecord) -> str:
        level_farbe = {
            "DEBUG": DUNKELGRAU,
            "INFO": CYAN,
            "WARNING": GELB,
            "ERROR": ROT,
            "CRITICAL": ROT,
        }.get(record.levelname, WEISS)
        level = farbe(f"{record.levelname:<8}", level_farbe, fett=True)
        nachricht = record.getMessage()
        if record.levelno >= logging.ERROR:
            nachricht = farbe(nachricht, ROT)
        elif record.levelno >= logging.WARNING:
            nachricht = farbe(nachricht, GELB)
        else:
            nachricht = farbe(nachricht, CYAN)
        return f"{level} | {nachricht}"


def erstelle_log_ordner(config: Config) -> Path:
    """Legt workdir/logs an (idempotent) und gibt den Pfad zurueck."""
    pfad = Path(config.general.log_dir)
    pfad.mkdir(parents=True, exist_ok=True)
    return pfad


def erstelle_logdatei_namen(basis_name: str, config: Config) -> str:
    """
    Logdateiname mit Sekunden-Zeitstempel.

    Der Zeitstempel im Namen ist kein Zierrat: zwei Laeufe in derselben Minute
    sollen sich nicht gegenseitig ueberschreiben.
    """
    ordner = erstelle_log_ordner(config)
    stempel = datetime.now().strftime("%Y-%m-%d_%H-%M-%S")
    return str(ordner / f"{stempel}-{basis_name}.log")


def setup_logging(config: Config, debug: bool = False) -> str:
    """
    Konfiguriert Datei- und Terminal-Handler und gibt den Logpfad zurueck.

    Handler werden vorher entfernt: die CLI kann mehrere Einzelschritte
    nacheinander ausfuehren, und ohne das Entfernen wuerde jede Zeile
    mehrfach geschrieben.
    """
    logger.handlers.clear()
    logger.setLevel(logging.DEBUG)
    logger.propagate = False

    log_datei = erstelle_logdatei_namen("whdload_workflow", config)

    datei_handler = logging.FileHandler(
        log_datei, mode="a", encoding=config.general.log_encoding
    )
    datei_handler.setLevel(logging.DEBUG)
    datei_handler.setFormatter(logging.Formatter(LOG_FORMAT_DATEI, LOG_DATUM))
    logger.addHandler(datei_handler)

    # stderr statt stdout: die eigentliche Ausgabe des Programms geht auf
    # stdout, Warnungen sollen auch dann sichtbar sein, wenn stdout in eine
    # Datei umgeleitet wird.
    terminal_handler = logging.StreamHandler(sys.stderr)
    terminal_handler.setLevel(logging.DEBUG if debug else logging.WARNING)
    terminal_handler.setFormatter(_FarbigesLevelFormatter())
    logger.addHandler(terminal_handler)

    logger.info(f"Logging initialisiert - Log-Datei: {log_datei}")
    logger.info(f"Debug-Modus: {'aktiviert' if debug else 'deaktiviert'}")
    logger.info(
        f"Terminal-Level: {'DEBUG' if debug else 'WARNING'}, Datei-Level: DEBUG"
    )
    return log_datei


class UI:
    """
    Einzige Stelle, die auf das Terminal schreibt.

    Jede Methode gibt ihren Text aus und spiegelt ihn anschliessend als
    "[UI] <Zeile>" ins Log. Dadurch ist das Log ein vollstaendiges Abbild
    dessen, was der Benutzer gesehen hat - ohne dass die Schritt-Funktionen
    zusaetzliche Logaufrufe brauchen.
    """

    def __init__(self, config: Config, breite: int | None = None):
        self.config = config
        self._breite = breite or terminal_breite()

    # --- interne Helfer ---
    def _log(self, *zeilen: str) -> None:
        """
        Spiegelt Terminalzeilen ins Log, ohne ANSI-Sequenzen.

        Die Konsole bekommt Farbe, das Log nicht: Steuercodes in einer
        Logdatei machen sie unlesbar und zerstoeren jedes grep-Muster.
        """
        for zeile in zeilen:
            if zeile.strip():
                logger.info(f"[UI] {_ANSI_MUSTER.sub('', zeile).rstrip()}")

    def _schreibe(self, text: str = "", loggen: bool = True) -> None:
        print(text)
        sys.stdout.flush()
        if loggen:
            self._log(text)

    def leerzeile(self) -> None:
        self._schreibe("")

    # --- Bausteine ---
    def banner(self) -> None:
        """Startbanner mit ASCII-Logo, Schrittuebersicht und Loghinweis."""
        logger.info("=== Banner angezeigt ===")
        logo = [
            "__        ___   _ ____  _     ___    _    ____  ",
            "\\ \\      / / | | |  _ \\| |   / _ \\  / \\  |  _ \\ ",
            " \\ \\ /\\ / /| |_| | | | | |  | | | |/ _ \\ | | | |",
            "  \\ V  V / |  _  | |_| | |__| |_| / ___ \\| |_| |",
            "   \\_/\\_/  |_| |_|____/|_____\\___/_/   \\_\\____/ ",
            "",
            "__        _____  ____  _  _______ _     _____        __",
            "\\ \\      / / _ \\|  _ \\| |/ /  ___| |   / _ \\ \\      / /",
            " \\ \\ /\\ / / | | | |_) | ' /| |_  | |  | | | \\ \\ /\\ / / ",
            "  \\ V  V /| |_| |  _ <| . \\|  _| | |__| |_| |\\ V  V /  ",
            "   \\_/\\_/  \\___/|_| \\_\\_|\\_\\_|   |_____\\___/  \\_/\\_/   ",
        ]
        inhalt: list[str] = []
        for zeile in logo:
            inhalt.append(farbe(zeile, CYAN, fett=True))
        inhalt.append("")
        schritte = [
            ("1", "FTP Mirror", "Laedt Archive vom FTP-Server"),
            ("2", "Sort", "Sortiert nach Amiga-System (500/1200/CD32/CDTV)"),
            ("3", "Compare", "Vergleicht zwei Verzeichnisse"),
            ("4", "Finalize", "Waehlt beste Sprachversionen aus"),
            ("5", "Cleanup", "Loescht temporaere Arbeitsordner"),
        ]
        for nummer, name, beschreibung in schritte:
            inhalt.append(
                f"  {farbe(nummer + '.', GELB, fett=True)} "
                f"{farbe(name, WEISS, fett=True)} {farbe('- ' + beschreibung, GRAU)}"
            )
        inhalt.append("")
        inhalt.append(
            farbe(
                f"  Logs werden im Unterordner '{LOG_DIR}' gespeichert.",
                GRAU,
                dunkel=True,
            )
        )
        self.panel(
            inhalt,
            titel=f"{PROGRAMM} v{VERSION}".strip(),
            untertitel=datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            farbe_rand=CYAN,
        )

    def panel(
        self,
        zeilen: list[str],
        titel: str = "",
        untertitel: str = "",
        farbe_rand: int = CYAN,
        breite: int | None = None,
    ) -> None:
        """
        Rahmen mit runden Ecken, optionalem Titel und Untertitel.

        Der Titel sitzt wie bei Rich in der oberen Rahmenlinie, der
        Untertitel rechts in der unteren. Die Innenbreite ist die
        Terminalsbreite minus Rahmen und Innenabstand.
        """
        aussen = breite or self._breite
        aussen = max(aussen, 24)
        innen = aussen - 4  # │ + Leerzeichen links/rechts + │

        kopf_titel = f" {titel} " if titel else ""
        kopf_titel = kuerze_auf_breite(kopf_titel, innen - 2)
        # Die obere Linie besteht aus "╭" + "─" + Titel + Füllung + "╮".
        # Damit sie genauso breit wie eine Inhaltszeile (│ + Text + │) endet,
        # braucht die Füllung ein Zeichen mehr als innen minus Titelbreite.
        fuell = innen - sichtbare_breite(kopf_titel) + 1
        oben = (
            farbe("╭─", farbe_rand)
            + farbe(kopf_titel, farbe_rand, fett=True)
            + farbe("─" * max(fuell, 0) + "╮", farbe_rand)
        )

        fuss_text = f" {untertitel} " if untertitel else ""
        fuss_text = kuerze_auf_breite(fuss_text, innen - 2)
        # Untere Linie: "╰" + Füllung + Untertitel + "╯" (zwei Zeichen mehr
        # als innen, weil der Untertitel nicht in die Innenbreite zählt).
        fuell_unten = innen - sichtbare_breite(fuss_text) + 2
        unten = (
            farbe("╰" + "─" * max(fuell_unten, 0), farbe_rand)
            + farbe(fuss_text, GRAU, dunkel=True)
            + farbe("╯", farbe_rand)
        )

        ausgabe = [oben]
        for zeile in zeilen:
            for teil in _zeilenumbruch(zeile, innen):
                ausgabe.append(
                    farbe("│", farbe_rand)
                    + " "
                    + _pad(teil, innen, "links")
                    + " "
                    + farbe("│", farbe_rand)
                )
        ausgabe.append(unten)
        self._schreibe("\n".join(ausgabe))

    def rule(self, titel: str = "", farbe_rand: int = DUNKELGRAU, fett: bool = False) -> None:
        """
        Trennlinie ueber die volle Breite, optional mit eingeruecktem Titel.

        Die Zeichenzahl wird aus sichtbare_breite() abgeleitet, damit die
        Linie bei Emoji im Titel nicht ueber den Rand hinauslaeuft.
        """
        gesamt = self._breite
        if not titel:
            self._schreibe(farbe("─" * gesamt, farbe_rand))
            return
        text = f" {titel} "
        rest = max(gesamt - sichtbare_breite(text), 2)
        links = rest // 2
        rechts = rest - links
        self._schreibe(
            farbe("─" * links, farbe_rand)
            + farbe(text, farbe_rand, fett=fett)
            + farbe("─" * rechts, farbe_rand)
        )

    def step_header(self, nummer: int, name: str, beschreibung: str) -> None:
        """Schrittkopf aus farbiger Rule, Beschreibung und Abschlussrule."""
        farbcode = {
            1: CYAN,
            2: BLAU,
            3: MAGENTA,
            4: GRUEN,
            5: GELB,
        }.get(nummer, WEISS)
        symbol = {
            1: "🌐",
            2: "📋",
            3: "🔍",
            4: "🎯",
            5: "🧹",
        }.get(nummer, "▸")
        self.leerzeile()
        self.rule(
            f"{symbol} SCHRITT {nummer}: {name}",
            farbe_rand=farbcode,
            fett=True,
        )
        self._schreibe(f"  {farbe(beschreibung, GRAU, dunkel=True)}")
        self.rule(farbe_rand=farbcode)

    def info_panel(self, titel: str, zeilen: list[tuple[str, str]]) -> None:
        """Panel mit Label/Wert-Paaren (Konfigurationsanzeige je Schritt)."""
        inhalt = []
        for label, wert in zeilen:
            inhalt.append(f"  {farbe(label + ':', WEISS, fett=True)} {wert}")
        self.panel(inhalt, titel=titel, farbe_rand=CYAN)

    def tabelle(
        self,
        titel: str,
        kopf: list[str],
        zeilen: list[list[str]],
        farbe_rand: int = DUNKELGRAU,
        ausrichtung: list[str] | None = None,
        kopf_farbe: int = WEISS,
        trennungen: list[int] | None = None,
    ) -> None:
        """
        Tabelle mit leichtem Rahmen.

        Spaltenbreiten ergeben sich aus dem breitesten Inhalt; das ist
        vorhersagbar und bleibt bei kleinen Terminals lesbar, weil jede
        Zelle notfalls gekuerzt wird.

        trennungen nennt Zeilenindizes, VOR denen eine Trennlinie gezeichnet
        wird. So entstehen Gruppen (z.B. Summenzeile), ohne zwischen allen
        Zeilen Linien zu ziehen - das waere unruhig und unleserlich.
        """
        if not kopf:
            return
        ausrichtung = ausrichtung or ["links"] * len(kopf)
        anzahl = len(kopf)
        trennungen = set(trennungen or [])

        breiten = [sichtbare_breite(k) + 2 for k in kopf]
        for zeile in zeilen:
            for i in range(anzahl):
                zelle = zeile[i] if i < len(zeile) else ""
                breiten[i] = max(breiten[i], sichtbare_breite(zelle) + 2)

        # Bei zu breiter Tabelle nur die breiteste Spalte schrumpfen, bis sie
        # ein Minimum erreicht. Eine gleichmaessige Kuerzung aller Spalten
        # wuerde kurze Spalten wie "Status" oder "Dauer" zerstoeren, obwohl
        # dort gar kein Platzproblem besteht.
        gesamt = sum(breiten) + anzahl + 1
        ueberstand = gesamt - self._breite
        while ueberstand > 0:
            index = max(range(anzahl), key=lambda i: breiten[i])
            if breiten[index] <= 8:
                break
            reduktion = min(ueberstand, breiten[index] - 8)
            breiten[index] -= reduktion
            ueberstand -= reduktion

        def rahmen(links: str, mitte: str, rechts: str) -> str:
            return (
                farbe(links, farbe_rand)
                + farbe(mitte, farbe_rand).join(
                    farbe("─" * b, farbe_rand) for b in breiten
                )
                + farbe(rechts, farbe_rand)
            )

        def zeile_bauen(zellen: list[str], fett: bool = False) -> str:
            teile = []
            for i, breite in enumerate(breiten):
                zelle = zellen[i] if i < len(zellen) else ""
                zelle = kuerze_auf_breite(zelle, breite - 2)
                zelle = _pad(zelle, breite - 2, ausrichtung[i])
                if fett and FARBEN_AKTIV:
                    zelle = farbe(zelle, kopf_farbe, fett=True)
                teile.append(" " + zelle + " ")
            return (
                farbe("│", farbe_rand)
                + farbe("│", farbe_rand).join(teile)
                + farbe("│", farbe_rand)
            )

        gesamt_titel = kuerze_auf_breite(titel, self._breite - 2)
        ausgabe = [f"  {farbe(gesamt_titel, kopf_farbe, fett=True)}"]
        ausgabe.append(rahmen("┌", "┬", "┐"))
        ausgabe.append(zeile_bauen(kopf, fett=True))
        ausgabe.append(rahmen("├", "┼", "┤"))
        for index, zeile in enumerate(zeilen):
            if index in trennungen:
                ausgabe.append(rahmen("├", "┼", "┤"))
            ausgabe.append(zeile_bauen(zeile))
        ausgabe.append(rahmen("└", "┴", "┘"))
        self._schreibe("\n".join(ausgabe))

    def config_table(self, titel: str, zeilen: list[tuple[str, str]]) -> None:
        """Zweispaltige Konfigurationstabelle (Label/Wert)."""
        self.tabelle(
            titel,
            ["Einstellung", "Wert"],
            [[label, wert] for label, wert in zeilen],
            farbe_rand=CYAN,
            kopf_farbe=CYAN,
        )

    def success(self, text: str) -> None:
        """Erfolgsmeldung mit Haken."""
        logger.info(f"OK: {text}")
        self._schreibe(f"  {farbe('✓', HELLGRUEN, fett=True)} {text}", loggen=False)
        self._log(f"  ✓ {text}")

    def detail(self, text: str) -> None:
        """Gedaempfte Detailzeile."""
        logger.info(f"DETAIL: {text}")
        self._schreibe(f"  {farbe(text, GRAU, dunkel=True)}", loggen=False)
        self._log(f"  {text}")

    def warning(self, text: str) -> None:
        """Warnung mit Symbol; landet zusaetzlich als WARNING im Log."""
        logger.warning(f"WARN: {text}")
        zeile = f"  {farbe('⚠', GELB, fett=True)} {farbe(text, GELB)}"
        self._schreibe(zeile, loggen=False)
        self._log(f"  ⚠ {text}")

    def error(self, text: str, titel: str = "FEHLER") -> None:
        """Fehlerpanel in Rot; im Log als ERROR."""
        logger.error(f"[{titel}] {text}")
        self.panel(
            [farbe(text, ROT)],
            titel=f"✗ {titel}",
            farbe_rand=ROT,
        )

    def summary_table(self, results: list[StepResult], log_datei: str | None = None) -> None:
        """
        Abschlusstabelle ueber alle Schritte.

        Der Gesamtstatus ist nur dann "Done", wenn kein Schritt ERROR ist.
        SKIPPED und DRY_RUN gelten als erfolgreich - ein bewusst
        uebersprungener Schritt ist kein Fehler.
        """
        if not results:
            return
        logger.info("=== Zusammenfassung wird angezeigt ===")
        for r in results:
            logger.info(
                f"  Schritt {r.number} ({r.name}): {r.status} - "
                f"{r.files_processed} Dateien, {r.duration_sec:.1f}s - {r.detail}"
            )

        zeilen: list[list[str]] = []
        for r in results:
            zeilen.append(
                [
                    f"{r.number}. {r.name}",
                    r.status_icon,
                    str(r.files_processed) if r.files_processed else "",
                    r.duration_str,
                    r.detail,
                ]
            )

        gesamt = sum(r.duration_sec for r in results)
        alles_ok = all(
            r.status in (StepStatus.OK, StepStatus.SKIPPED, StepStatus.DRY_RUN)
            for r in results
        )
        zeilen.append(
            [
                farbe("Gesamt", WEISS, fett=True),
                farbe("✓ Done", HELLGRUEN, fett=True)
                if alles_ok
                else farbe("✗ Error", ROT, fett=True),
                "",
                farbe(formatiere_zeitdauer(gesamt, kurz=True), WEISS, fett=True),
                f"Log: {log_datei}" if log_datei else "",
            ]
        )

        self.leerzeile()
        self.tabelle(
            f"{PROGRAMM} - Zusammenfassung",
            ["Schritt", "Status", "Dateien", "Dauer", "Hinweise"],
            zeilen,
            farbe_rand=CYAN,
            ausrichtung=["links", "mitte", "rechts", "rechts", "links"],
            kopf_farbe=CYAN,
            trennungen=[len(results)],
        )
        self.leerzeile()

    # --- interaktive Eingaben ---
    def confirm(self, frage: str, default: bool = True) -> bool:
        """
        Ja/Nein-Frage im Stil von 2.0 ("[J/n]"), tolerant bei der Eingabe.

        Akzeptiert j/ja/y/yes und n/nein/no. Bei fehlender Eingabe gilt der
        Default. Wenn stdin nicht interaktiv ist (Pipe, CI), wird der Default
        NICHT blind verwendet: bei destruktiven Fragen waere das gefaehrlich,
        deshalb gibt es dort immer "Nein" und einen Hinweis.
        """
        standard_text = "J/n" if default else "j/N"
        prompt_text = (
            f"\n{frage} "
            f"{farbe(f'[{standard_text}]', GRAU, dunkel=True)}: "
        )
        try:
            eingabe = input(prompt_text).strip().lower()
        except EOFError:
            self.warning(
                "Keine interaktive Eingabe moeglich - Frage wird mit 'Nein' beantwortet."
            )
            return False
        if not eingabe:
            return default
        if eingabe in ("j", "ja", "y", "yes"):
            return True
        if eingabe in ("n", "nein", "no"):
            return False
        self.warning(f"Ungueltige Eingabe '{eingabe}' - bitte j oder n.")
        return self.confirm(frage, default)

    # --- Fortschritt ---
    def fortschritt(self, beschreibung: str, gesamt: int) -> "Fortschrittsbalken":
        """Erzeugt einen Fortschrittsbalken; als Kontextmanager zu benutzen."""
        return Fortschrittsbalken(self, beschreibung, gesamt)

    def status(self, text: str) -> "StatusSpinner":
        """Animierter Spinner fuer Prozesse ohne bekannte Gesamtmenge."""
        return StatusSpinner(self, text)

    def _balken_zeile(
        self,
        beschreibung: str,
        spinner: str,
        anteil: float,
        fertig: int,
        gesamt: int,
        sekunden: float,
    ) -> str:
        """
        Eine Zeile des Fortschrittsbalkens.

        Aufbau wie bei 2.0: Spinner, Beschreibung, Balken, Prozent,
        KB-Angabe, KB/s, Restzeit. Die KB-Angabe ist bewusst fest und nicht
        auto-skaliert, weil die Groessenordnung hier immer im KB- bis
        MB-Bereich liegt und springende Einheiten unruhig wirken.
        """
        breite = self._breite
        anteil = max(0.0, min(anteil, 1.0))
        rest_text = self._restzeit(sekunden, anteil)
        kb = _format_kb(fertig, gesamt)
        kb_s = f"{fertig / 1024 / sekunden:.1f} KB/s" if sekunden > 0.2 else ""

        # Rechte Seite zuerst: sie ist die wichtigste Information. Optionale
        # Teile (Geschwindigkeit, Restzeit) kommen nur dazu, wenn danach noch
        # genug Platz fuer Beschreibung und Balken bleibt. Sonst wuerde die
        # Zeile in schmalen Terminals umbrechen.
        rechts_teile = [f"{anteil * 100:3.0f}%", kb]
        for optional in (kb_s, rest_text):
            if not optional:
                continue
            kandidat = rechts_teile + [optional]
            if sichtbare_breite(" ".join(kandidat)) <= breite - 30:
                rechts_teile.append(optional)
        rechts = " " + " ".join(rechts_teile)

        # Beschreibung so kuerzen, dass Spinner, Beschreibung, ein Balken von
        # mindestens vier Zellen und die rechte Seite zusammen passen.
        platz_beschreibung = max(6, breite - sichtbare_breite(rechts) - 8)
        beschreibung = kuerze_auf_breite(beschreibung, platz_beschreibung)
        links = f"{spinner} {beschreibung} "
        balken_breite = max(4, breite - sichtbare_breite(links) - sichtbare_breite(rechts))
        voll = int(balken_breite * anteil)
        balken = (
            farbe(BALKEN_VOLL * voll, CYAN)
            + farbe(BALKEN_LEER * (balken_breite - voll), DUNKELGRAU)
        )
        return links + balken + rechts

    @staticmethod
    def _restzeit(sekunden: float, anteil: float) -> str:
        if anteil <= 0.01 or sekunden <= 1:
            return ""
        rest = sekunden * (1 - anteil) / anteil
        if rest >= 3600:
            return f"{int(rest // 3600)}h{int((rest % 3600) // 60):02d}m"
        if rest >= 60:
            return f"{int(rest // 60)}m{int(rest % 60):02d}s"
        return f"{int(rest)}s"


def _format_kb(fertig: int, gesamt: int) -> str:
    """KB-Anzeige im Format von 2.0: 'fertig/gesamt KB' oder nur 'fertig KB'."""
    if gesamt:
        return f"{fertig / 1024:.1f}/{gesamt / 1024:.1f} KB"
    return f"{fertig / 1024:.1f} KB"


class Fortschrittsbalken:
    """
    Fortschrittsbalken mit echter Terminalsausgabe.

    In einem Terminal wird dieselbe Zeile per Wagenruecklauf neu gezeichnet
    (maximal 10 Mal pro Sekunde, sonst flackert es). In einer Pipe gibt es
    keine Steuerzeichen; dort wird nur am Ende eine Zusammenfassung
    geschrieben, damit Logdateien lesbar bleiben.
    """

    def __init__(self, ui: UI, beschreibung: str, gesamt: int):
        self.ui = ui
        self.beschreibung = beschreibung
        self.gesamt = max(int(gesamt), 1)
        self.fertig = 0
        self._start = time.time()
        self._letzte_zeichnung = 0.0
        self._interaktiv = sys.stdout.isatty() and FARBEN_AKTIV
        self._frames = 0

    def __enter__(self) -> "Fortschrittsbalken":
        self._start = time.time()
        if self._interaktiv:
            self._zeichne()
        return self

    def advance(self, schritt: int) -> None:
        """Erhoeht den Fortschritt um `schritt` Einheiten."""
        self.fertig = min(self.fertig + max(int(schritt), 0), self.gesamt)
        if not self._interaktiv:
            return
        jetzt = time.time()
        if jetzt - self._letzte_zeichnung >= 0.1 or self.fertig >= self.gesamt:
            self._letzte_zeichnung = jetzt
            self._zeichne()

    def _zeichne(self) -> None:
        self._frames += 1
        spinner = SPINNER[self._frames % len(SPINNER)]
        sekunden = max(time.time() - self._start, 0.001)
        zeile = self.ui._balken_zeile(
            self.beschreibung,
            spinner,
            self.fertig / self.gesamt,
            self.fertig,
            self.gesamt,
            sekunden,
        )
        # \r plus Leerzeile am Ende der Zeile raeumt Reste der Vorgaengerzeile weg.
        sys.stdout.write("\r" + zeile + " " * max(0, self.ui._breite - sichtbare_breite(zeile)))
        sys.stdout.flush()

    def __exit__(self, *exc_info) -> None:
        dauer = max(time.time() - self._start, 0.001)
        if self._interaktiv:
            self._zeichne()
            sys.stdout.write("\n")
            sys.stdout.flush()
        self.ui._log(
            f"  {self.beschreibung} {self.fertig / 1024:.1f} KB "
            f"in {dauer:.1f}s"
        )


class StatusSpinner:
    """
    Spinner fuer unbekannte Gesamtdauer (z.B. rekursiver FTP-Mirror).

    Der Spinner laeuft in einem eigenen Thread, weil die Arbeit im
    Hauptthread blockiert. Der Thread ist ein Daemon und wird ueber ein
    Event beendet; damit bleibt das Programm auch bei einer Exception
    sauber terminiert.
    """

    def __init__(self, ui: UI, text: str):
        self.ui = ui
        self.text = text
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._interaktiv = sys.stdout.isatty() and FARBEN_AKTIV

    def __enter__(self) -> "StatusSpinner":
        if self._interaktiv:
            self._thread = threading.Thread(target=self._lauf, daemon=True)
            self._thread.start()
        else:
            self.ui._schreibe(f"  {self.text}")
        return self

    def _lauf(self) -> None:
        frames = 0
        while not self._stop.is_set():
            spinner = SPINNER[frames % len(SPINNER)]
            zeile = f"{spinner} {self.text}"
            sys.stdout.write("\r" + zeile + " " * max(0, self.ui._breite - sichtbare_breite(zeile)))
            sys.stdout.flush()
            frames += 1
            self._stop.wait(0.08)

    def __exit__(self, *exc_info) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=0.5)
            sys.stdout.write("\r" + " " * self.ui._breite + "\r")
            sys.stdout.flush()
        self.ui._log(f"  {self.text} - abgeschlossen")


def formatiere_zeitdauer(sekunden: float, kurz: bool = False) -> str:
    """
    Sekunden in lesbare Dauer.

    kurz=True liefert die Variante fuer Tabellenzellen ("1m 5s"),
    kurz=False die ausfuehrliche Variante ("1h 5m") fuer Fliesstext.
    """
    if sekunden < 60:
        return f"{sekunden:.1f}s"
    if sekunden < 3600:
        return f"{int(sekunden // 60)}m {int(sekunden % 60)}s"
    stunden = int(sekunden // 3600)
    minuten = int((sekunden % 3600) // 60)
    if kurz:
        return f"{stunden}h {minuten}m"
    return f"{stunden}h {minuten}m"

# ============================================================================
# === 3. WORKDIR & DATEI-HANDLING ============================================
# ============================================================================


def formatiere_dategroesse(bytes_anzahl: int) -> str:
    """
    Bytezahl in lesbare Einheit (1024er-Basis).

    Die Grenzen sind strikt: 1024 B sind 1.0 KB, nicht 1024 B. Das klingt
    selbstverstaendlich, ist aber die Stelle, an der Anzeige und
    Fortschrittsbalken gern auseinanderlaufen.
    """
    if bytes_anzahl < 1024:
        return f"{bytes_anzahl} B"
    if bytes_anzahl < 1024**2:
        return f"{bytes_anzahl / 1024:.1f} KB"
    if bytes_anzahl < 1024**3:
        return f"{bytes_anzahl / (1024**2):.1f} MB"
    return f"{bytes_anzahl / (1024**3):.2f} GB"


def kuerze_pfad(pfad_text: str, max_laenge: int, position: str = "middle") -> str:
    """
    Kuerzt langen Text mit Ellipse, ohne den echten Pfad zu beruehren.

    "middle" ist der Default, weil bei FTP-Pfaden Anfang (Serverbasis) und
    Ende (Dateiname) informativ sind und die Mitte am wenigsten.
    """
    if max_laenge <= 0 or len(pfad_text) <= max_laenge:
        return pfad_text
    if max_laenge <= 3:
        return pfad_text[:max_laenge]
    if position == "left":
        return "…" + pfad_text[-(max_laenge - 1) :]
    if position == "right":
        return pfad_text[: max_laenge - 1] + "…"
    links = (max_laenge - 1) // 2
    rechts = max_laenge - 1 - links
    return pfad_text[:links] + "…" + pfad_text[-rechts:]


def formatiere_anzeige_pfad(remote_pfad: str, config: Config) -> str:
    """
    Verkuerzt einen Remote-Pfad nur fuer die Anzeige.

    Der zurueckgegebene String wird niemals an ftplib weitergereicht; dort
    gilt immer der Originalpfad. Diese Trennung ist der Grund, warum das
    Umschreiben gefahrlos ist.
    """
    pfad = str(remote_pfad)
    dc = config.display
    if dc.path_rewrite_enabled and dc.path_rewrite_from:
        if pfad.startswith(dc.path_rewrite_from):
            pfad = dc.path_rewrite_to + pfad[len(dc.path_rewrite_from) :]
    if dc.path_maxlen > 0:
        pfad = kuerze_pfad(pfad, dc.path_maxlen, dc.path_ellipsis)
    return pfad


def berechne_ordner_groesse_bytes(ordner: str) -> int:
    """
    Summierte Dateigroesse eines Ordners.

    Nicht lesbare Dateien werden uebersprungen: eine einzelne Datei mit
    fehlenden Rechten darf die Groessenanzeige nicht komplett scheitern
    lassen.
    """
    gesamt = 0
    for wurzel, _ordner, dateien in os.walk(ordner):
        for datei in dateien:
            try:
                gesamt += os.path.getsize(os.path.join(wurzel, datei))
            except OSError:
                pass
    return gesamt


def berechne_ordner_groesse_gb(ordner: str) -> float:
    """Ordnergroesse in GiB (1024er-Basis, wie df sie fuer Bytes liefert)."""
    return berechne_ordner_groesse_bytes(ordner) / (1024**3)


def sammle_dateinamen(verzeichnis: str) -> set[str]:
    """
    Alle Dateinamen rekursiv, ohne Pfade und ohne Duplikate.

    Nur Namen, weil Compare die Frage beantwortet "ist jede Datei da?",
    nicht "liegt sie am selben Ort?". Genau diese Semantik hat 1.3.
    """
    namen: set[str] = set()
    for _wurzel, _ordner, dateien in os.walk(verzeichnis):
        for datei in dateien:
            namen.add(datei)
    return namen


def sammle_dateinamen_mit_pfaden(verzeichnis: str) -> dict[str, list[str]]:
    """
    Dateiname -> Liste relativer Pfade.

    Eine Liste, weil derselbe Name in mehreren Unterordnern liegen kann
    (bei Amiga 500 zum Beispiel A/ und B/). Die Diff-Ausgabe zeigt dann
    alle Fundstellen.
    """
    ergebnis: dict[str, list[str]] = {}
    for wurzel, _ordner, dateien in os.walk(verzeichnis):
        for datei in dateien:
            voll = os.path.join(wurzel, datei)
            relativ = os.path.relpath(voll, verzeichnis)
            ergebnis.setdefault(datei, []).append(relativ)
    return ergebnis


# --- Hashing ---------------------------------------------------------------


def _lese_chunkweise(dateipfad: str, verarbeite) -> None:
    """
    Liest eine Datei in 1-MiB-Bloecken und ruft `verarbeite` je Block auf.

    Der Chunk-Ansatz haelt den Speicher konstant; ein 2-GB-Archiv waere
    sonst ein 2-GB-`read()`.
    """
    with open(dateipfad, "rb") as datei:
        for block in iter(lambda: datei.read(1024 * 1024), b""):
            verarbeite(block)


def berechne_lokalen_hash(dateipfad: str, algorithmus: str) -> str:
    """
    Hash einer lokalen Datei; unterstuetzt CRC32 und alle hashlib-Algorithmen.

    CRC32 ist kein hashlib-Algorithmus und wird ueber zlib berechnet. Die
    Maske 0xFFFFFFFF erzwingt ein unsigned 32-bit-Ergebnis, weil zlib auf
    manchen Plattformen ein negatives int liefert.
    """
    algo = algorithmus.replace("-", "").lower()
    if algo == "crc32":
        crc = 0
        with open(dateipfad, "rb") as datei:
            for block in iter(lambda: datei.read(1024 * 1024), b""):
                crc = zlib.crc32(block, crc)
        return f"{crc & 0xFFFFFFFF:08x}"
    hasher = hashlib.new(algo)
    _lese_chunkweise(dateipfad, hasher.update)
    return hasher.hexdigest()


def vergleiche_dateigroesse(lokaler_pfad: str, remote_groesse: int | None) -> str | None:
    """
    Schwache Verifikation als Fallback, wenn der Server keinen Hash kann.

    Ein Groessenvergleich erkennt keine inhaltliche Korruption gleicher
    Laenge, aber er erkennt abgebrochene Downloads - und mehr ist ohne
    Serverhash nicht zu haben.
    """
    if not os.path.exists(lokaler_pfad) or not remote_groesse:
        return None
    return "SIZE_ONLY" if os.path.getsize(lokaler_pfad) == remote_groesse else "SIZE_MISMATCH"


# --- Dateinamen-Parser ------------------------------------------------------
#
# Format der Retroplay-Archive:
#     SpielName_vVersion_Sprachen_Zusatzinfo_Zahlencode.lha
#     z.B. AlienBreed3D_v1.3_AGA_0624.lha
#
# Beide Parser teilen die Vorverarbeitung, interpretieren den Rest aber
# unterschiedlich. Das ist kein Zufall, sondern notwendig:
#   - Sort braucht nur AGA/CD32/CDTV, also reicht "alle Teile ab Index 1".
#   - Finalize muss Versionen desselben Spiels gruppieren. Dafuer ist die
#     Version der Trennmarker: alles davor ist Spielname, alles danach ist
#     Sprache oder Zusatzinfo. Wuerde man wie beim Sort alle Teile lesen,
#     landete "v1.0" als Zusatzinfo in den Schluesseln und die Gruppierung
#     waere kaputt.


def _parse_basis(dateiname: str, config: Config) -> list[str] | None:
    """
    Gemeinsame Vorverarbeitung: Endung pruefen, Zahlencode entfernen, teilen.

    Der Zahlencode (_2024, _2023&2024) wird entfernt, weil er nur die
    Sortierreihenfolge des Servers abbildet und bei Varianten desselben
    Spiels unterschiedlich ist.
    """
    endung = config.general.archive_extension
    if not dateiname.endswith(endung):
        return None
    ohne_endung = dateiname[: -len(endung)]
    bereinigt = re.sub(config.general.zahlencode_pattern, "", ohne_endung)
    teile = bereinigt.split("_")
    return teile if teile else None


def parse_sort(dateiname: str, config: Config) -> dict | None:
    """
    Parser fuer den Sort-Schritt.

    Jeder Teil ab Index 1 wird klassifiziert:
      v/V...      -> Version
      Gross-Klein -> Sprachcode (De, En, DeEn)
      sonst       -> Zusatzinfo (AGA, CD32, HiRes, ...)
    """
    teile = _parse_basis(dateiname, config)
    if teile is None:
        return None

    version: str | None = None
    sprachen: list[str] = []
    zusatzinfo: list[str] = []

    for teil in teile[1:]:
        if teil.startswith("v") or teil.startswith("V"):
            version = teil
        elif re.match(config.general.sprachcode_pattern, teil):
            sprachen.extend(re.findall(r"[A-Z][a-z]", teil))
        else:
            zusatzinfo.append(teil)

    # Jede Datei braucht mindestens eine Sprache, sonst kann Finalize sie
    # spaeter keiner Sprachgruppe zuordnen.
    if not sprachen:
        sprachen = [config.finalize.default_language]

    return {
        "filename": dateiname,
        "game_name": teile[0],
        "version": version,
        "languages": sprachen,
        "additional_info": zusatzinfo,
    }


def parse_finalize(dateiname: str, config: Config) -> dict | None:
    """
    Parser fuer den Finalize-Schritt.

    Zusaetzlich zu parse_sort entstehen version_key und full_key:
      version_key = Spiel_Version_sortierteSprachen
      full_key    = version_key + Zusatzinfos
    Beide werden von der Auswahl nicht direkt gebraucht, sind aber die
    dokumentierte Gruppensemantik aus 1.3/2.0 und werden deshalb beibehalten.
    """
    teile = _parse_basis(dateiname, config)
    if teile is None:
        return None

    ergebnis: dict = {
        "original": dateiname,
        "game_name": teile[0],
        "version": None,
        "language_codes": [],
        "additional_info": [],
        "raw_parts": teile,
    }

    # Nur die ERSTE Version zaehlt als Trennmarker. Ein zweites "v..." im
    # Namen ist Zusatzinfo und darf die Spracherkennung nicht verschieben.
    versions_idx = -1
    for i, teil in enumerate(teile[1:], 1):
        if teil.startswith("v") or teil.startswith("V"):
            ergebnis["version"] = teil
            versions_idx = i
            break

    if versions_idx > 0:
        for teil in teile[versions_idx + 1 :]:
            if re.match(config.general.sprachcode_pattern, teil):
                ergebnis["language_codes"].extend(re.findall(r"[A-Z][a-z]", teil))
            else:
                ergebnis["additional_info"].append(teil)

    if not ergebnis["language_codes"]:
        ergebnis["language_codes"] = [config.finalize.default_language]

    version = ergebnis["version"] or "noversion"
    sprachen = "".join(sorted(ergebnis["language_codes"]))
    ergebnis["version_key"] = f"{ergebnis['game_name']}_{version}_{sprachen}"
    ergebnis["full_key"] = (
        f"{ergebnis['version_key']}_{'_'.join(ergebnis['additional_info'])}"
    )
    return ergebnis


# --- Sicherheitsguards ------------------------------------------------------


def _ist_im_workdir(pfad: str) -> bool:
    """
    Prueft, ob ein Pfad echt innerhalb von ./workdir liegt.

    resolve() loest "..", Symlinks und relative Anteile auf, bevor
    verglichen wird. Ohne das wuerde "workdir/../.." als harmlos gelten.
    Der Vergleich nutzt commonpath statt startswith, weil startswith bei
    "workdir-alt" faelschlich true liefern wuerde.
    """
    try:
        wurzel = Path(WORKDIR).resolve()
        ziel = Path(pfad).resolve()
    except OSError:
        return False
    try:
        return os.path.commonpath([str(wurzel), str(ziel)]) == str(wurzel) and ziel != wurzel
    except ValueError:
        return False


def _pruefe_quell_ziel(quelle: str, ziel: str) -> str | None:
    """
    Verhindert Konfigurationen, die Daten zerstoeren koennten.

    Zwei Faelle sind kritisch:
      - Quelle == Ziel: Kopien ueberschreiben sich selbst
      - Ziel liegt in der Quelle: der naechste Lauf kopiert seine eigenen
        Ergebnisse erneut und waechst unbegrenzt
    Beides wird abgelehnt, bevor irgendeine Datei angefasst wird.
    """
    try:
        q = Path(quelle).resolve()
        z = Path(ziel).resolve()
    except OSError:
        return None
    if q == z:
        return f"Quell- und Zielordner sind identisch: {quelle}"
    try:
        if os.path.commonpath([str(q), str(z)]) == str(q):
            return f"Zielordner '{ziel}' liegt innerhalb der Quelle '{quelle}'"
    except ValueError:
        pass
    return None

# ============================================================================
# === 4. WHDLOAD KERNLOGIK (FTP) =============================================
# ============================================================================
#
# Der FTP-Teil ist der einzige, der mit unzuverlaessigem Gegenueber spricht.
# Deshalb stecken hier alle Wiederholungs- und Wiederaufnahme-Mechanismen:
#   - Verbindungsaufbau mit linearem Backoff
#   - NOOP-Keepalive vor jedem Listing, Reconnect bei Verlust
#   - MLSD mit NLST-Fallback (alte Server kennen MLSD nicht)
#   - Resume via REST, Skip bei identischer Groesse
#   - Hash-Verifikation mit Groessenvergleich als Rueckfallebene


class FTPClient:
    """
    Kapselt die gesamte FTP-Kommunikation des Mirror-Schritts.

    Der Client kennt die Statistik-Dataclass als Referenz und schreibt
    waehrend des Downloads hinein. Das erspart Rueckgabewerte durch die
    Rekursion und entspricht dem Verhalten von 1.3/2.0.
    """

    def __init__(self, config: Config, stats: MirrorStats):
        self._config = config
        self._stats = stats
        self._ftp: ftplib.FTP | None = None

    @property
    def ftp(self) -> ftplib.FTP:
        """Aktive Verbindung; ein Zugriff ohne Verbindung ist ein Programmierfehler."""
        if self._ftp is None:
            raise FTPConnectionError("FTP-Verbindung nicht aktiv")
        return self._ftp

    def connect(self) -> None:
        """
        Verbindet mit bis zu max_retries Versuchen.

        Der Backoff ist linear (2s, 4s, 6s), nicht exponentiell: der Server
        ist ein oeffentlicher Mirrorserver, kein Dienst mit Rate-Limit, und
        lange Wartezeiten verzoegern den Abbruch unnoetig.
        """
        fc = self._config.ftp
        letzter_fehler: Exception | None = None

        for versuch in range(1, fc.max_retries + 1):
            try:
                ftp = ftplib.FTP(timeout=fc.timeout)
                ftp.connect(fc.host, fc.port, timeout=fc.timeout)
                ftp.login(fc.user, fc.password)
                ftp.set_pasv(fc.use_passive)

                # Keepalive auf Socket-Ebene: viele Router verwerfen
                # Verbindungen, die 30-60 s stumm bleiben.
                try:
                    ftp.sock.setsockopt(socket.SOL_SOCKET, socket.SO_KEEPALIVE, 1)
                except Exception:
                    pass  # nicht kritisch

                # Binaermodus: ohne TYPE I rechnen manche Server mit
                # Zeilenendenumwandlung und liefern korrupte Archive.
                try:
                    ftp.voidcmd("TYPE I")
                except Exception:
                    pass

                self._ftp = ftp
                logger.info(f"FTP verbunden mit {fc.host}:{fc.port} (Versuch {versuch})")
                return
            except Exception as fehler:
                letzter_fehler = fehler
                logger.warning(
                    f"FTP Verbindungsversuch {versuch}/{fc.max_retries} "
                    f"fehlgeschlagen: {fehler}"
                )
                if versuch < fc.max_retries:
                    time.sleep(fc.sleep_between_retries * versuch)

        logger.error(f"FTP-Verbindung nach {fc.max_retries} Versuchen fehlgeschlagen")
        raise FTPConnectionError(
            f"FTP-Verbindung zu {fc.host}:{fc.port} nach {fc.max_retries} "
            f"Versuchen fehlgeschlagen: {letzter_fehler}"
        )

    def reconnect(self) -> None:
        """Schliesst die alte Verbindung und baut eine neue inklusive FEAT auf."""
        logger.info("FTP: Reconnect")
        if self._ftp is not None:
            try:
                self._ftp.close()
            except Exception:
                pass
        self._ftp = None
        self.connect()
        # FEAT gilt pro Verbindung, die Hash-Faehigkeit muss neu ermittelt
        # werden.
        self.setup_hash_funktionen()

    def keepalive(self) -> None:
        """
        Haelt die Verbindung mit NOOP wach.

        Schlaegt NOOP fehl, ist die Verbindung tot und ein Reconnect noetig;
        ohne diesen Test wuerde erst das naechste CWD scheitern und ein
        Retry-Zaehler verbraucht.
        """
        try:
            self.ftp.voidcmd("NOOP")
        except Exception:
            logger.warning("FTP: Keepalive fehlgeschlagen - Reconnect")
            self.reconnect()

    def setup_hash_funktionen(self) -> None:
        """
        Ermittelt HASH- und XCRC-Unterstuetzung ueber FEAT.

        Der Server nennt seine Algorithmen in einer Zeile wie
        "HASH SHA-256;*;MD5". Wir waehlen den ersten bevorzugten, den er
        anbietet, und aktivieren ihn mit OPTS HASH. Schlaegt das fehl, bleibt
        es beim Groessenvergleich.
        """
        self._stats.hash_supported = False
        self._stats.hash_algo = None
        self._stats.xcrc_supported = False

        try:
            feat = self.ftp.sendcmd("FEAT")
            zeilen = [z.strip() for z in feat.splitlines()]
        except Exception:
            logger.debug("FTP: FEAT nicht unterstuetzt - keine Hash-Verifikation")
            return

        hash_zeile = next((z for z in zeilen if z.upper().startswith("HASH")), None)
        if hash_zeile:
            tokens = hash_zeile.split()
            if len(tokens) >= 2:
                angeboten = [
                    teil.replace("*", "").strip() for teil in tokens[1].split(";")
                ]
                for bevorzugt in self._config.ftp.preferred_hash_algos:
                    if bevorzugt in angeboten:
                        try:
                            self.ftp.sendcmd(f"OPTS HASH {bevorzugt}")
                            self._stats.hash_supported = True
                            self._stats.hash_algo = bevorzugt
                            logger.info(f"FTP: Hash-Verifikation aktiv: {bevorzugt}")
                            break
                        except Exception:
                            continue

        for zeile in zeilen:
            gross = zeile.upper()
            if "XCRC" in gross or ("SITE" in gross and "CRC" in gross):
                self._stats.xcrc_supported = True
                break

    def dateigroesse(self, remote_pfad: str, fakten: dict | None = None) -> int | None:
        """
        Groesse einer Remote-Datei.

        MLSD-Fakten zuerst: sie kosten keinen zusaetzlichen Roundtrip und
        sind die einzige Quelle, wenn der Server SIZE nicht beherrscht.
        """
        if fakten:
            groesse = fakten.get("size") or fakten.get("sizd")
            if groesse and str(groesse).isdigit():
                return int(groesse)

        fc = self._config.ftp
        for versuch in range(1, fc.max_retries + 1):
            try:
                return self.ftp.size(remote_pfad)
            except Exception:
                if versuch < fc.max_retries:
                    time.sleep(fc.sleep_between_retries)
        return None

    def _teste_ordner(self, name: str) -> bool:
        """
        Typbestimmung per CWD-Test (nur fuer NLST-Listings noetig).

        Ein Verzeichnis laesst sich betreten, eine Datei nicht. Das ist
        langsam, aber die einzige Moeglichkeit ohne MLSD.
        """
        try:
            aktuell = self.ftp.pwd()
        except Exception:
            return False
        try:
            self.ftp.cwd(name)
            self.ftp.cwd(aktuell)
            return True
        except Exception:
            return False

    def list_directory(self, remote_ordner: str) -> list[tuple[str, str, dict]]:
        """
        Verzeichnisinhalt als Liste (Name, "file"/"dir", MLSD-Fakten).

        Reihenfolge wird alphabetisch sortiert. Der Server liefert zwar meist
        sortiert, garantiert das aber nicht; eine feste Reihenfolge macht
        Laeufe reproduzierbar und Logs vergleichbar.

        Die Wiederherstellung des Arbeitsverzeichnisses steht im finally,
        damit ein Abbruch mitten im Listing die Verbindung nicht in einem
        unbekannten Zustand zuruecklaesst.
        """
        self.keepalive()
        fc = self._config.ftp

        for versuch in range(1, fc.max_retries + 1):
            aktueller_ordner: str | None = None
            try:
                try:
                    aktueller_ordner = self.ftp.pwd()
                except Exception:
                    self.reconnect()
                    aktueller_ordner = None

                self.ftp.cwd(remote_ordner)
                eintraege: list[tuple[str, str, dict]] = []

                try:
                    for name, fakten in self.ftp.mlsd():
                        if name in (".", ".."):
                            continue
                        typ = (fakten.get("type") or "").lower()
                        if typ in ("dir", "cdir", "pdir"):
                            eintraege.append((name, "dir", fakten))
                        elif typ == "file":
                            eintraege.append((name, "file", fakten))
                        else:
                            art = "dir" if self._teste_ordner(name) else "file"
                            eintraege.append((name, art, fakten))
                    logger.debug(f"FTP: MLSD '{remote_ordner}' - {len(eintraege)} Eintraege")
                    return eintraege
                except Exception:
                    logger.debug(f"FTP: MLSD nicht verfuegbar, NLST fuer '{remote_ordner}'")
                    try:
                        namen = self.ftp.nlst()
                    except Exception:
                        namen = []
                    eintraege = []
                    for name in namen:
                        if name in (".", ".."):
                            continue
                        art = "dir" if self._teste_ordner(name) else "file"
                        eintraege.append((name, art, {}))
                    return eintraege

            except Exception as fehler:
                logger.warning(
                    f"FTP: Listing-Versuch {versuch} fuer '{remote_ordner}' "
                    f"fehlgeschlagen: {fehler}"
                )
                if versuch < fc.max_retries:
                    self.reconnect()
                    time.sleep(fc.sleep_between_retries)
                    continue
                self._stats.errors.append(
                    f"Listing fehlgeschlagen fuer "
                    f"'{formatiere_anzeige_pfad(remote_ordner, self._config)}': {fehler}"
                )
                return []
            finally:
                if aktueller_ordner:
                    try:
                        self.ftp.cwd(aktueller_ordner)
                    except Exception:
                        pass

        return []

    def download_file(
        self, remote_pfad: str, lokaler_pfad: str, fakten: dict | None = None
    ) -> DownloadStats:
        """
        Laedt eine Datei herunter und verifiziert sie.

        Ablauf:
          1. Groesse ermitteln
          2. Skip, wenn lokal gleich gross (spart Bandbreite)
          3. Resume, wenn lokal kleiner (REST ab aktueller Position)
          4. Download
          5. Verifikation: Hash wenn moeglich, sonst Groesse
          6. mtime aus MLSD uebernehmen

        Der Rueckgabewert beschreibt nur diese eine Datei; die
        Gesamtstatistik wird direkt in self._stats fortgeschrieben.
        """
        fc = self._config.ftp
        stats = DownloadStats()
        remote_groesse = self.dateigroesse(remote_pfad, fakten)

        if os.path.exists(lokaler_pfad):
            lokale_groesse = os.path.getsize(lokaler_pfad)
            if (
                fc.skip_existing_same_size
                and remote_groesse
                and lokale_groesse == remote_groesse
            ):
                self._stats.files_synced += 1
                self._stats.files_skipped += 1
                stats.action = "skipped"
                logger.debug(
                    f"FTP: Skip '{remote_pfad}' - identische Groesse ({lokale_groesse} B)"
                )
                return stats

        start_position = 0
        modus = "downloaded"
        if fc.resume_partial and os.path.exists(lokaler_pfad):
            start_position = os.path.getsize(lokaler_pfad)
            # Nur fortsetzen, wenn die lokale Datei kleiner ist. Ist sie
            # groesser als das Original, ist sie kaputt (oder stammt von einer
            # anderen Version) und wird komplett neu geladen.
            if start_position > 0 and (not remote_groesse or start_position < remote_groesse):
                modus = "resumed"
                logger.debug(f"FTP: Resume '{remote_pfad}' ab {start_position} B")
            elif start_position > 0:
                logger.warning(
                    f"FTP: '{remote_pfad}' lokal groesser als remote - wird neu geladen"
                )
                start_position = 0

        bytes_geladen = 0
        start_zeit = time.time()

        try:
            # Der Oeffnungsmodus haengt am MODUS, nicht an start_position:
            # 1.3/2.0 haengten bei jeder vorhandenen Datei an ('ab') und
            # zerstoerten damit Archive, die lokal groesser waren als das
            # Original. Anhaengen ist ausschliesslich beim Resume richtig.
            with open(lokaler_pfad, "ab" if modus == "resumed" else "wb") as datei:
                if modus == "resumed" and start_position > 0:
                    self.ftp.voidcmd(f"REST {start_position}")

                def callback(daten: bytes) -> None:
                    nonlocal bytes_geladen
                    datei.write(daten)
                    bytes_geladen += len(daten)

                self.ftp.retrbinary(f"RETR {remote_pfad}", callback, fc.blocksize)
        except Exception as fehler:
            self._stats.errors.append(f"Download fehlgeschlagen: {remote_pfad}: {fehler}")
            logger.error(f"FTP: Download fehlgeschlagen: {remote_pfad}: {fehler}")
            raise

        dauer = max(time.time() - start_zeit, 0.001)
        self._stats.bytes_downloaded += bytes_geladen
        self._stats.transfer_seconds += dauer
        self._stats.files_synced += 1
        if modus == "resumed":
            self._stats.files_resumed += 1

        stats.action = modus
        stats.bytes_downloaded = bytes_geladen
        stats.secs = dauer
        stats.mbps = (bytes_geladen / (1024 * 1024)) / dauer
        logger.debug(
            f"FTP: {modus} '{remote_pfad}' - {bytes_geladen} B in {dauer:.1f}s"
        )

        stats.verify = self._verifiziere(lokaler_pfad, remote_pfad, remote_groesse)
        logger.debug(f"FTP: Verifikation '{remote_pfad}' - {stats.verify}")

        if fc.set_mtime and fakten and "modify" in fakten:
            self._setze_mtime(lokaler_pfad, fakten["modify"])

        return stats

    def _verifiziere(
        self, lokaler_pfad: str, remote_pfad: str, remote_groesse: int | None
    ) -> str:
        """
        Verifikation mit abgestufter Genauigkeit.

        Reihenfolge: Serverhash -> Groessenvergleich -> "unbekannt". Jede
        Stufe ist schwaecher als die vorige; das Ergebnis benennt die Stufe,
        damit im Log erkennbar bleibt, wie sicher der Nachweis war.
        """
        if not self._config.ftp.verify_hash:
            return "SKIPPED"

        if self._stats.hash_supported and self._stats.hash_algo:
            try:
                lokaler_hash = berechne_lokalen_hash(lokaler_pfad, self._stats.hash_algo)
            except Exception:
                logger.error(f"FTP: Hash-Berechnung fehlgeschlagen: {lokaler_pfad}")
                return "ERROR"
            try:
                antwort = self.ftp.sendcmd(f"HASH {remote_pfad}")
                remote_hash = antwort.split()[-1].lower()
                if lokaler_hash.lower() == remote_hash:
                    return "OK"
                logger.warning(f"FTP: Hash-Verifikation FEHLGESCHLAGEN: {remote_pfad}")
                return "FAILED"
            except Exception:
                # HASH-Kommando gescheitert, Groessenvergleich als Ersatz.
                return vergleiche_dateigroesse(lokaler_pfad, remote_groesse) or "UNAVAILABLE"

        if not os.path.exists(lokaler_pfad):
            return "SKIPPED"
        if remote_groesse:
            return vergleiche_dateigroesse(lokaler_pfad, remote_groesse) or "SIZE_MISMATCH"
        try:
            nachgemessen = self.ftp.size(remote_pfad)
        except Exception:
            return "NO_REMOTE_SIZE"
        if not nachgemessen:
            return "NO_REMOTE_SIZE"
        return vergleiche_dateigroesse(lokaler_pfad, nachgemessen) or "SIZE_MISMATCH"

    @staticmethod
    def _setze_mtime(lokaler_pfad: str, modify_str: str) -> None:
        """
        Uebernimmt den Server-Zeitstempel.

        MLSD liefert UTC ohne Zeitzonenangabe; ohne tzinfo=timezone.utc
        wuerde die lokale Zeitzone eingerechnet und jedes Archiv waere um
        Stunden verschoben.
        """
        try:
            zeitpunkt = datetime.strptime(modify_str, "%Y%m%d%H%M%S").replace(
                tzinfo=timezone.utc
            )
            stempel = zeitpunkt.timestamp()
            os.utime(lokaler_pfad, (stempel, stempel))
        except Exception:
            pass

    def mirror_folder(self, basis_remote: str, basis_lokal: str) -> None:
        """
        Rekursive Spiegelung eines Remote-Ordners.

        Der Basisremote wird vereinheitlicht (genau ein fuehrender Slash),
        weil os.path.relpath sonst je nach Schreibweise unterschiedliche
        relative Pfade liefert und der lokale Baum verschachtelt wuerde.
        """
        normalisiert = normpath("/" + basis_remote.lstrip("/"))
        logger.info(f"FTP: Spiegelung {normalisiert} -> {basis_lokal}")
        self._rekursiv_spiegeln(normalisiert, basis_remote, basis_lokal)
        logger.info(
            f"FTP: fertig - {self._stats.folders_synced} Ordner, "
            f"{self._stats.files_synced} Dateien, "
            f"{self._stats.bytes_downloaded / (1024 * 1024):.1f} MB"
        )

    def _rekursiv_spiegeln(
        self, remote_ordner: str, basis_remote: str, basis_lokal: str
    ) -> None:
        """
        Tiefensuche: erst alle Unterordner, dann die Dateien dieses Ordners.

        Reihenfolge ist fachlich egal (jede Datei landet am gleichen Platz),
        beeinflusst aber die Logreihenfolge. Tiefensuche entspricht 1.3/2.0.
        """
        relativ = os.path.relpath(remote_ordner, basis_remote)
        lokaler_ordner = (
            basis_lokal if relativ == "." else os.path.join(basis_lokal, relativ)
        )
        os.makedirs(lokaler_ordner, exist_ok=True)
        self._stats.folders_synced += 1

        eintraege = self.list_directory(remote_ordner)
        ordner = [(n, f) for (n, t, f) in eintraege if t == "dir"]
        dateien = [(n, f) for (n, t, f) in eintraege if t == "file"]

        for name, _fakten in ordner:
            self._rekursiv_spiegeln(normpath(pjoin(remote_ordner, name)), basis_remote, basis_lokal)

        for name, fakten in dateien:
            self._rekursiv_datei(remote_ordner, name, fakten, lokaler_ordner)

    def _rekursiv_datei(
        self, remote_ordner: str, name: str, fakten: dict, lokaler_ordner: str
    ) -> None:
        """
        Einzeldownload innerhalb der Spiegelung, mit Fehlerisolierung.

        Ein kaputtes Archiv darf den restlichen Lauf nicht abbrechen; der
        Fehler wird gezaehlt und gesammelt, der Schritt endet dann mit
        Status ERROR, nachdem alles andere geladen wurde.
        """
        remote_pfad = normpath(pjoin(remote_ordner, name))
        lokaler_pfad = os.path.join(lokaler_ordner, name)
        try:
            self.download_file(remote_pfad, lokaler_pfad, fakten)
        except Exception as fehler:
            self._stats.errors.append(f"{remote_pfad}: {fehler}")
            self._stats.files_error += 1
            logger.error(f"FTP: Download-Fehler '{remote_pfad}': {fehler}")

    def quit(self) -> None:
        """QUIT, bei Fehler CLOSE; die Verbindung ist danach immer weg."""
        if self._ftp is None:
            return
        try:
            self._ftp.quit()
            logger.info("FTP: Verbindung geschlossen (QUIT)")
        except Exception:
            try:
                self._ftp.close()
                logger.info("FTP: Verbindung geschlossen (CLOSE)")
            except Exception:
                pass
        self._ftp = None

# ============================================================================
# === 5. DIE FUENF SCHRITTE ==================================================
# ============================================================================
#
# Jeder Schritt hat eine run_*-Funktion, die Konfiguration und UI bekommt und
# ein StepResult liefert. Die Fachfunktionen darunter sind einzeln aufrufbar
# und kennen die UI nur, weil sie Fortschritt und Tabellen anzeigen muessen.


def run_mirror(config: Config, ui: UI, dry_run: bool = False) -> tuple[StepResult, MirrorStats]:
    """
    Schritt 1: FTP Mirror.

    Der Dry-Run verbindet sich NICHT und laedt nichts; er zeigt nur die
    Konfiguration. Ein "Trockenlauf" gegen einen fremden Server waere kein
    Trockenlauf.
    """
    logger.info("=== Schritt 1: FTP MIRROR gestartet ===")
    fc = config.ftp
    stats = MirrorStats()
    ui.step_header(1, "FTP MIRROR", f"Laedt WHDLoad-Archive von {fc.host} herunter")

    ui.info_panel(
        "Mirror-Konfiguration",
        [
            ("Remote-Pfad", formatiere_anzeige_pfad(fc.remote_path, config)),
            ("Lokaler Zielordner", config.mirror_folder),
            ("Passivmodus", "Ja" if fc.use_passive else "Nein"),
            ("Hash-Verifikation", fc.verify_hash),
            ("Dry-Run", "Ja (kein Download)" if dry_run else "Nein"),
        ],
    )

    start = datetime.now()

    if dry_run:
        ui.warning("Dry-Run aktiv - es wird keine Verbindung aufgebaut und nichts geladen.")
        return (
            StepResult(
                number=1,
                name="FTP Mirror",
                status=StepStatus.DRY_RUN,
                duration_sec=(datetime.now() - start).total_seconds(),
                detail="Dry-Run - kein Download",
            ),
            stats,
        )

    logger.info(f"Verbinde zu FTP-Server: {fc.host}:{fc.port}")
    ui.detail("Verbinde zu FTP-Server...")
    client = FTPClient(config, stats)
    try:
        client.connect()
        ui.success("Verbindung hergestellt!")
        client.setup_hash_funktionen()
        if stats.hash_supported:
            ui.detail(f"Hash-Verifikation verfuegbar: {stats.hash_algo}")
        if stats.xcrc_supported:
            ui.detail("XCRC verfuegbar")

        with ui.status("Spiegle FTP-Ordner..."):
            client.mirror_folder(fc.remote_path, config.mirror_folder)
    except FTPConnectionError as fehler:
        ui.error(str(fehler), "FTP-Verbindungsfehler")
        logger.exception("FTP-Verbindungsfehler")
        return (
            StepResult(
                number=1,
                name="FTP Mirror",
                status=StepStatus.ERROR,
                duration_sec=(datetime.now() - start).total_seconds(),
                errors=[str(fehler)],
                detail=str(fehler),
            ),
            stats,
        )
    finally:
        client.quit()

    dauer = (datetime.now() - start).total_seconds()
    zeilen = [
        ("Startzeit", start.strftime("%Y-%m-%d %H:%M:%S")),
        ("Endzeit", datetime.now().strftime("%Y-%m-%d %H:%M:%S")),
        ("Gesamtdauer", formatiere_zeitdauer(dauer)),
        ("Ordner verarbeitet", str(stats.folders_synced)),
        ("Dateien verarbeitet", str(stats.files_synced)),
        ("Davon uebersprungen", str(stats.files_skipped)),
        ("Davon resumed", str(stats.files_resumed)),
        ("Fehler", str(stats.files_error)),
        ("Daten heruntergeladen", formatiere_dategroesse(stats.bytes_downloaded)),
    ]
    if stats.transfer_seconds > 0:
        zeilen.append(("Ø Download-Geschwindigkeit", f"{stats.avg_mbps:.2f} MB/s"))
    zeilen.append(
        (
            f"Speicherbelegung '{config.mirror_folder}'",
            f"{berechne_ordner_groesse_gb(config.mirror_folder):.2f} GB",
        )
    )
    ui.config_table("Mirror abgeschlossen - Statistik", zeilen)

    status = StepStatus.OK if stats.files_error == 0 else StepStatus.ERROR
    details = [
        f"{stats.files_synced} Dateien",
        formatiere_dategroesse(stats.bytes_downloaded),
    ]
    if stats.files_error:
        details.append(f"{stats.files_error} Fehler")
    return (
        StepResult(
            number=1,
            name="FTP Mirror",
            status=status,
            duration_sec=dauer,
            files_processed=stats.files_synced,
            detail=", ".join(details),
            errors=stats.errors[:10],
        ),
        stats,
    )


def sortiere_nach_system(
    quellordner: str, zielordner: str, config: Config, ui: UI, kopieren: bool = True
) -> SortStats:
    """
    Verteilt die Archive auf die vier Systemordner.

    Die Zuordnung ist eine Kette, keine Menge: AGA gewinnt gegen CD32, CD32
    gegen CDTV. Ein Archiv, das mehrere Kennungen traegt, landet damit
    deterministisch im ersten passenden Ordner - genau wie in 1.3/2.0.
    """
    sc = config.sort
    stats = SortStats()
    quelle = Path(quellordner)
    ziel = Path(zielordner)

    if not quelle.exists():
        ui.error(f"Quellordner '{quellordner}' existiert nicht!")
        return stats

    konflikt = _pruefe_quell_ziel(quellordner, zielordner)
    if konflikt:
        ui.error(konflikt)
        return stats

    ziele = {
        "amiga1200": ziel / sc.folder_amiga1200,
        "amigacd32": ziel / sc.folder_amigacd32,
        "amigacdtv": ziel / sc.folder_amigacdtv,
        "amiga500": ziel / sc.folder_amiga500,
    }

    ui.detail("Erstelle Zielordner...")
    for name, pfad in ziele.items():
        if kopieren:
            pfad.mkdir(parents=True, exist_ok=True)
        ui.success(name)

    endung = config.general.archive_extension
    # Bewusst KEIN sorted(): die Reihenfolge bestimmt, in welcher Folge die
    # Zieldateien angelegt werden, und damit die Verzeichnisreihenfolge des
    # sort-Baums. Genau diese Reihenfolge entscheidet in Finalize ueber den
    # Sprach-Fallback (siehe waehle_beste_versionen), deshalb muss sie mit
    # 1.3/2.0 identisch bleiben.
    dateien = list(quelle.rglob(f"*{endung}"))
    stats.total_files = len(dateien)
    ui.detail(f"Gefunden: {len(dateien)} {endung}-Dateien")

    gesamt_bytes = 0
    for pfad in dateien:
        try:
            gesamt_bytes += pfad.stat().st_size
        except OSError:
            pass

    with ui.fortschritt("Sortiere Dateien...", gesamt_bytes or len(dateien)) as balken:
        for pfad in dateien:
            try:
                groesse = pfad.stat().st_size
            except OSError:
                groesse = 0
            # Fortschritt auch bei uebersprungenen Dateien weiterruecken,
            # sonst bleibt der Balken am Ende stehen.
            fortschritt = groesse if gesamt_bytes else 1

            info = parse_sort(pfad.name, config)
            if not info:
                balken.advance(fortschritt)
                continue

            for zusatz in info["additional_info"]:
                stats.additional_info_counts[zusatz] = (
                    stats.additional_info_counts.get(zusatz, 0) + 1
                )
            for sprache in info["languages"]:
                stats.language_counts[sprache] = (
                    stats.language_counts.get(sprache, 0) + 1
                )

            kennungen = [z.upper() for z in info["additional_info"]]
            if "AGA" in kennungen:
                kategorie = "amiga1200"
            elif "CD32" in kennungen:
                kategorie = "amigacd32"
            elif "CDTV" in kennungen:
                kategorie = "amigacdtv"
            else:
                kategorie = "amiga500"

            ziel_ordner_datei = ziele[kategorie]
            # Amiga 500 behaelt die alphabetische Serverstruktur (A/, B/, ...),
            # weil dort mehrere tausend Dateien liegen und ein flacher Ordner
            # unuebersichtlich waere. AGA/CD32/CDTV sind klein genug fuer flach.
            if kategorie == "amiga500" and pfad.parent != quelle:
                relativ = pfad.parent.relative_to(quelle)
                ziel_ordner_datei = ziel_ordner_datei / relativ
                if kopieren:
                    ziel_ordner_datei.mkdir(parents=True, exist_ok=True)

            ziel_datei = ziel_ordner_datei / pfad.name
            if kopieren:
                try:
                    # copy2 statt copy: mtimes der Archive bleiben erhalten und
                    # damit die Sortierung im Dateimanager nachvollziehbar.
                    shutil.copy2(pfad, ziel_datei)
                    setattr(stats, kategorie, getattr(stats, kategorie) + 1)
                except Exception as fehler:
                    logger.error(f"FEHLER beim Kopieren von {pfad.name}: {fehler}")
                    stats.errors += 1
            else:
                setattr(stats, kategorie, getattr(stats, kategorie) + 1)

            balken.advance(fortschritt)

    logger.info(
        f"Sort: 1200={stats.amiga1200} CD32={stats.amigacd32} "
        f"CDTV={stats.amigacdtv} 500={stats.amiga500} Fehler={stats.errors}"
    )
    return stats


def run_sort(config: Config, ui: UI, dry_run: bool = False) -> StepResult:
    """
    Schritt 2: Sort.

    Wichtig: Im Dry-Run wird wirklich nicht kopiert. 2.0 hat den Status
    DRY_RUN gemeldet, aber trotzdem geschrieben; dieser Fehler wird hier
    bewusst nicht uebernommen.
    """
    sc = config.sort
    logger.info("=== Schritt 2: SORT gestartet ===")
    ui.step_header(2, "SORT", "Sortiert Archive nach Amiga-Systemtyp (500/1200/CD32/CDTV)")
    ui.info_panel(
        "Sort-Konfiguration",
        [
            ("Quellordner", sc.source_dir),
            ("Zielordner", sc.target_dir),
            ("Dry-Run", "Ja (nur Analyse)" if dry_run else "Nein"),
        ],
    )

    # Ohne Quelle gibt es nichts zu sortieren. Im Trockenlauf ist das die
    # logische Folge von Schritt 1 (nichts geladen) und kein Fehler.
    if not Path(sc.source_dir).exists():
        ui.warning(f"Quellordner fehlt: {sc.source_dir}")
        return StepResult(
            number=2,
            name="Sort",
            status=StepStatus.DRY_RUN if dry_run else StepStatus.ERROR,
            detail=f"Quellordner fehlt: {sc.source_dir}",
        )

    start = datetime.now()
    if dry_run:
        ui.warning("Dry-Run: Dateien werden nur analysiert, nicht kopiert.")

    stats = sortiere_nach_system(sc.source_dir, sc.target_dir, config, ui, kopieren=not dry_run)
    dauer = (datetime.now() - start).total_seconds()

    ui.tabelle(
        "Sortierung abgeschlossen - Statistik",
        ["System", "Dateien"],
        [
            ["Amiga 1200 (AGA)", str(stats.amiga1200)],
            ["Amiga CD32", str(stats.amigacd32)],
            ["Amiga CDTV", str(stats.amigacdtv)],
            ["Amiga 500 (OCS)", str(stats.amiga500)],
            [
                "Gesamt",
                str(stats.amiga1200 + stats.amigacd32 + stats.amigacdtv + stats.amiga500),
            ],
            ["Fehler", str(stats.errors)],
        ],
        farbe_rand=BLAU,
        ausrichtung=["links", "rechts"],
        kopf_farbe=BLAU,
        trennungen=[4],
    )

    if stats.additional_info_counts:
        top = sorted(stats.additional_info_counts.items(), key=lambda x: x[1], reverse=True)[:10]
        ui.tabelle(
            "Top 10 Zusatzinformationen",
            ["Info", "Anzahl"],
            [[info, str(anzahl)] for info, anzahl in top],
            ausrichtung=["links", "rechts"],
        )

    if stats.language_counts:
        sprachen = sorted(stats.language_counts.items(), key=lambda x: x[1], reverse=True)
        ui.tabelle(
            "Sprachen gefunden",
            ["Sprache", "Anzahl"],
            [[sprache, str(anzahl)] for sprache, anzahl in sprachen],
            ausrichtung=["links", "rechts"],
        )

    status = StepStatus.OK if stats.errors == 0 else StepStatus.ERROR
    if dry_run:
        status = StepStatus.DRY_RUN
    return StepResult(
        number=2,
        name="Sort",
        status=status,
        duration_sec=dauer,
        files_processed=stats.total_files,
        detail=(
            f"1200:{stats.amiga1200} CD32:{stats.amigacd32} "
            f"CDTV:{stats.amigacdtv} 500:{stats.amiga500}"
        ),
    )


def vergleiche_verzeichnisse(ordner1: str, ordner2: str, config: Config, ui: UI) -> CompareStats:
    """
    Schritt 3 als Fachfunktion: reine Namensmengen, keine Bytevergleiche.

    Das ist bewusst schwach: Compare beantwortet "fehlt eine Datei?", nicht
    "ist sie unbeschaedigt?". Fuer die Integritaet ist der Hash im
    Mirror-Schritt zustaendig.
    """
    stats = CompareStats()
    if not os.path.isdir(ordner1):
        ui.error(f"'{ordner1}' ist kein gueltiges Verzeichnis!")
        return stats
    if not os.path.isdir(ordner2):
        ui.error(f"'{ordner2}' ist kein gueltiges Verzeichnis!")
        return stats

    dateien1 = sammle_dateinamen(ordner1)
    dateien2 = sammle_dateinamen(ordner2)
    nur1 = dateien1 - dateien2
    nur2 = dateien2 - dateien1
    gemeinsam = dateien1 & dateien2

    stats.files_dir1 = len(dateien1)
    stats.files_dir2 = len(dateien2)
    stats.common = len(gemeinsam)
    stats.only_in_dir1 = sorted(nur1)
    stats.only_in_dir2 = sorted(nur2)

    ui.tabelle(
        "Vergleichs-Statistik",
        ["Metrik", "Anzahl"],
        [
            ["Dateien in Ordner 1", str(stats.files_dir1)],
            ["Dateien in Ordner 2", str(stats.files_dir2)],
            ["Gemeinsame Dateien", str(stats.common)],
            ["Nur in Ordner 1", str(len(nur1))],
            ["Nur in Ordner 2", str(len(nur2))],
        ],
        farbe_rand=MAGENTA,
        ausrichtung=["links", "rechts"],
        kopf_farbe=MAGENTA,
    )

    if nur1:
        ui.tabelle(
            f"Dateien nur in {ordner1}",
            ["Dateiname"],
            [[name] for name in sorted(nur1)],
            farbe_rand=ROT,
            kopf_farbe=ROT,
        )
    if nur2:
        ui.tabelle(
            f"Dateien nur in {ordner2}",
            ["Dateiname"],
            [[name] for name in sorted(nur2)],
            farbe_rand=GELB,
            kopf_farbe=GELB,
        )
    if not nur1 and not nur2:
        ui.success("Beide Verzeichnisse enthalten die gleichen Dateinamen!")

    if nur1 or nur2:
        ui.rule("Detaillierte Ausgabe mit Pfaden")
        pfade1 = sammle_dateinamen_mit_pfaden(ordner1)
        pfade2 = sammle_dateinamen_mit_pfaden(ordner2)
        stats.paths_dir1 = pfade1
        stats.paths_dir2 = pfade2
        if nur1:
            ui.tabelle(
                f"Pfade nur in {ordner1}",
                ["Pfad"],
                [[p] for name in sorted(nur1) for p in pfade1.get(name, [])],
                farbe_rand=ROT,
                kopf_farbe=ROT,
            )
        if nur2:
            ui.tabelle(
                f"Pfade nur in {ordner2}",
                ["Pfad"],
                [[p] for name in sorted(nur2) for p in pfade2.get(name, [])],
                farbe_rand=GELB,
                kopf_farbe=GELB,
            )
    return stats


def run_compare(
    config: Config, ui: UI, dir1: str | None = None, dir2: str | None = None
) -> StepResult:
    """Schritt 3: Compare mit optionalen CLI-Overrides."""
    cc = config.compare
    o1 = dir1 or cc.default_dir1
    o2 = dir2 or cc.default_dir2
    logger.info("=== Schritt 3: COMPARE gestartet ===")
    ui.step_header(3, "COMPARE", "Vergleicht zwei Verzeichnisse und zeigt Unterschiede")
    ui.info_panel("Compare-Konfiguration", [("Ordner 1", o1), ("Ordner 2", o2)])

    # Ohne gueltige Verzeichnisse ist "keine Unterschiede" eine Luege:
    # zwei fehlende Ordner sind nicht gleich, sie sind nicht vergleichbar.
    fehlend = [p for p in (o1, o2) if not os.path.isdir(p)]
    if fehlend:
        ui.error(f"Verzeichnis fehlt: {', '.join(fehlend)}")
        return StepResult(
            number=3,
            name="Compare",
            status=StepStatus.ERROR,
            detail=f"Verzeichnis fehlt: {', '.join(fehlend)}",
        )

    start = datetime.now()
    stats = vergleiche_verzeichnisse(o1, o2, config, ui)
    dauer = (datetime.now() - start).total_seconds()

    unterschiede = len(stats.only_in_dir1) + len(stats.only_in_dir2)
    detail = f"{unterschiede} Unterschiede" if unterschiede else "Keine Unterschiede"
    return StepResult(
        number=3,
        name="Compare",
        status=StepStatus.OK,
        duration_sec=dauer,
        files_processed=stats.files_dir1 + stats.files_dir2,
        detail=detail,
    )


def waehle_beste_versionen(spiel_dateien: list[dict], config: Config) -> tuple[list[dict], list[dict]]:
    """
    Waehlt die Sprachversionen eines Spiels aus.

    Die Regel ist bewusst grob und genau die von 1.3/2.0:
      1. Sprachen in Prioritaetsreihenfolge durchgehen
      2. erste Sprache mit mindestens einem Treffer gewinnt
      3. ALLE Dateien mit dieser Sprache bleiben

    Warum nicht nur die hoechste Version? Weil die Versionen im Archivnamen
    nicht zuverlaessig vergleichbar sind (v1.3 vs v1.3a vs v2.0 De). Eine
    falsche Versionswahl waere schlimmer als zwei Varianten zu behalten.
    """
    nach_sprache: dict[str, list[dict]] = defaultdict(list)
    for datei in spiel_dateien:
        nach_sprache["".join(sorted(datei["language_codes"]))].append(datei)

    ausgewaehlt: list[dict] = []
    entfernt: list[dict] = []

    for prio in config.finalize.language_priority:
        passende = [f for f in spiel_dateien if prio in f["language_codes"]]
        if passende:
            ausgewaehlt = passende
            entfernt = [f for f in spiel_dateien if f not in passende]
            break

    if not ausgewaehlt:
        # Keine Prioritaetssprache vorhanden (z.B. nur "It"): die erste
        # Sprachgruppe behalten, damit nie ein Spiel komplett verschwindet.
        erster_key = list(nach_sprache.keys())[0]
        ausgewaehlt = nach_sprache[erster_key]
        entfernt = [f for f in spiel_dateien if f not in ausgewaehlt]

    return ausgewaehlt, entfernt


def analysiere_finalize_ordner(
    quellordner: Path, unterordner: str, config: Config
) -> list[dict] | None:
    """
    Sammelt und parst alle Archive eines Plattform-Ordners.

    Rueckgabe None, wenn der Ordner fehlt: das unterscheidet "Plattform
    nicht vorhanden" (normal) von "Plattform vorhanden, aber leer" (Hinweis).
    """
    ordner_pfad = quellordner / unterordner
    if not ordner_pfad.exists():
        logger.debug(f"Finalize: '{unterordner}' existiert nicht")
        return None

    dateien: list[dict] = []
    for wurzel, _ordner, dateinamen in os.walk(ordner_pfad):
        # Kein sorted(): waehle_beste_versionen nimmt bei fehlender
        # Prioritaetssprache die ERSTE Sprachgruppe in Fundreihenfolge.
        # Umsortieren wuerde dort eine andere Sprache gewinnen lassen.
        for dateiname in dateinamen:
            if not dateiname.endswith(config.general.archive_extension):
                continue
            geparst = parse_finalize(dateiname, config)
            if not geparst:
                continue
            voll = os.path.join(wurzel, dateiname)
            geparst["full_path"] = voll
            geparst["relative_path"] = os.path.relpath(voll, ordner_pfad)
            geparst["subdir"] = unterordner
            dateien.append(geparst)

    return dateien


def verarbeite_finalize_unterordner(
    quellordner: Path,
    zielordner: Path,
    unterordner: str,
    config: Config,
    ui: UI,
    dateien_kopieren: bool = True,
) -> FinalizePlatformResult | None:
    """
    Ein Plattform-Ordner: analysieren, auswaehlen, anzeigen, kopieren.
    """
    fc = config.finalize
    ui.rule(f"Verarbeite: {unterordner}", farbe_rand=GRUEN, fett=True)
    result = FinalizePlatformResult(subdir=unterordner)

    dateien = analysiere_finalize_ordner(quellordner, unterordner, config)
    if not dateien:
        ui.warning(f"Keine {config.general.archive_extension}-Dateien in {unterordner} gefunden.")
        return None

    result.total = len(dateien)
    ui.detail(f"Gefundene Dateien: {len(dateien)}")

    spiele: dict[str, list[dict]] = defaultdict(list)
    for datei in dateien:
        spiele[datei["game_name"]].append(datei)
    ui.detail(f"Einzigartige Spiele: {len(spiele)}")

    sprach_stats: dict[str, int] = defaultdict(int)
    spiele_nach_sprache: dict[str, set] = defaultdict(set)
    for datei in dateien:
        for sprache in datei["language_codes"]:
            sprach_stats[sprache] += 1
            spiele_nach_sprache[sprache].add(datei["game_name"])
    result.lang_stats = dict(sprach_stats)
    result.games_by_lang = {k: sorted(v) for k, v in spiele_nach_sprache.items()}

    ui.tabelle(
        f"Sprachstatistik - {unterordner}",
        ["Sprache", "Dateien", "Spiele"],
        [
            [sprache, str(anzahl), str(len(spiele_nach_sprache[sprache]))]
            for sprache, anzahl in sorted(sprach_stats.items(), key=lambda x: x[1], reverse=True)
        ],
        ausrichtung=["links", "rechts", "rechts"],
    )

    alle_ausgewaehlt: list[dict] = []
    alle_entfernt: list[dict] = []
    mehrfach: list[tuple[str, list[dict], list[dict]]] = []

    for spiel_name, spiel_dateien in spiele.items():
        ausgewaehlt, entfernt = waehle_beste_versionen(spiel_dateien, config)
        alle_ausgewaehlt.extend(ausgewaehlt)
        alle_entfernt.extend(entfernt)
        if len(spiel_dateien) > 1:
            mehrfach.append((spiel_name, ausgewaehlt, entfernt))

    result.selected = alle_ausgewaehlt
    result.removed = alle_entfernt
    result.games = dict(spiele)

    ui.rule(f"Versionsauswahl - {len(mehrfach)} Spiele mit mehreren Versionen")
    for spielname, ausgewaehlt, entfernt in mehrfach:
        zeilen = []
        for datei in ausgewaehlt:
            zeilen.append(
                [
                    farbe("✓", HELLGRUEN, fett=True),
                    datei["original"],
                    datei["version"] or "unbekannt",
                    ", ".join(datei["language_codes"]),
                    ", ".join(datei["additional_info"]),
                ]
            )
        for datei in entfernt:
            zeilen.append(
                [
                    farbe("✗", ROT, fett=True),
                    datei["original"],
                    datei["version"] or "unbekannt",
                    ", ".join(datei["language_codes"]),
                    ", ".join(datei["additional_info"]),
                ]
            )
        ui.tabelle(
            spielname,
            ["Status", "Dateiname", "Version", "Sprachen", "Zusatz"],
            zeilen,
            ausrichtung=["mitte", "links", "links", "links", "links"],
        )

    prozent = (len(alle_entfernt) / len(dateien)) * 100 if dateien else 0.0
    ui.tabelle(
        f"Auswahl-Statistik - {unterordner}",
        ["Metrik", "Wert"],
        [
            ["Ausgewaehlt", str(len(alle_ausgewaehlt))],
            ["Entfernt", str(len(alle_entfernt))],
            ["Einsparung", f"{prozent:.1f}%"],
        ],
        farbe_rand=GRUEN,
        ausrichtung=["links", "rechts"],
        kopf_farbe=GRUEN,
    )

    if dateien_kopieren:
        ziel_unterordner = zielordner / unterordner
        ziel_unterordner.mkdir(parents=True, exist_ok=True)
        for datei in alle_ausgewaehlt:
            quelle = Path(datei["full_path"])
            name = datei["original"]
            if fc.underscores_to_spaces:
                name = name.replace("_", " ")

            if unterordner == "amiga500":
                relativ = Path(datei["relative_path"])
                if len(relativ.parts) > 1:
                    ziel_ordner_datei = ziel_unterordner / relativ.parent
                    ziel_ordner_datei.mkdir(parents=True, exist_ok=True)
                else:
                    ziel_ordner_datei = ziel_unterordner
            else:
                ziel_ordner_datei = ziel_unterordner

            try:
                shutil.copy2(quelle, ziel_ordner_datei / name)
                result.files_copied += 1
            except Exception as fehler:
                logger.error(f"FEHLER: {datei['original']}: {fehler}")
                result.copy_errors.append(f"{datei['original']}: {fehler}")

        ui.success(f"Erfolgreich kopiert: {result.files_copied} Dateien")

    return result


def run_finalize(config: Config, ui: UI, dry_run: bool = False) -> StepResult:
    """Schritt 4: Finalize ueber alle Plattform-Ordner."""
    fc = config.finalize
    logger.info("=== Schritt 4: FINALIZE gestartet ===")
    ui.step_header(4, "FINALIZE", "Waehlt beste Sprachversionen aus und erstellt finale Struktur")
    ui.info_panel(
        "Finalize-Konfiguration",
        [
            ("Modus", "SIMULATION (keine Dateiaktion)" if dry_run else "KOPIEREN AKTIVIERT"),
            ("Quellordner", fc.source_dir),
            ("Zielordner", fc.target_dir),
            ("Sprach-Prioritaet", " > ".join(fc.language_priority)),
        ],
    )

    quelle = Path(fc.source_dir)
    ziel = Path(fc.target_dir)
    if not quelle.exists():
        ui.error(f"Quellordner '{fc.source_dir}' existiert nicht!")
        return StepResult(
            number=4,
            name="Finalize",
            status=StepStatus.ERROR,
            detail=f"Quellordner fehlt: {fc.source_dir}",
        )
    konflikt = _pruefe_quell_ziel(fc.source_dir, fc.target_dir)
    if konflikt:
        ui.error(konflikt)
        return StepResult(
            number=4, name="Finalize", status=StepStatus.ERROR, detail=konflikt
        )

    start = datetime.now()
    stats = FinalizeStats()
    for unterordner in fc.platform_folders:
        ergebnis = verarbeite_finalize_unterordner(
            quelle, ziel, unterordner, config, ui, dateien_kopieren=not dry_run
        )
        if ergebnis:
            stats.platforms.append(ergebnis)

    dauer = (datetime.now() - start).total_seconds()

    zeilen = [
        ["Plattformen verarbeitet", str(len(stats.platforms))],
        ["Dateien gesamt", str(stats.total_files)],
        ["Ausgewaehlt", str(stats.total_selected)],
        ["Entfernt", str(stats.total_removed)],
        ["Einsparung", f"{stats.savings_percent:.1f}%"],
    ]
    if not dry_run:
        zeilen.append(["Kopiert", str(stats.total_copied)])
    ui.tabelle(
        "Finalize abgeschlossen - Gesamtstatistik",
        ["Metrik", "Wert"],
        zeilen,
        farbe_rand=GRUEN,
        ausrichtung=["links", "rechts"],
        kopf_farbe=GRUEN,
    )

    status = StepStatus.DRY_RUN if dry_run else StepStatus.OK
    details = [
        f"{stats.total_selected} ausgewaehlt",
        f"{stats.total_removed} entfernt",
        f"{stats.savings_percent:.1f}% Einsparung",
    ]
    if dry_run:
        details.append("Simulation")
    return StepResult(
        number=4,
        name="Finalize",
        status=status,
        duration_sec=dauer,
        files_processed=stats.total_selected,
        detail=", ".join(details),
    )


def run_cleanup(
    config: Config, ui: UI, force: bool = False, dry_run: bool = False
) -> StepResult:
    """
    Schritt 5: Cleanup.

    Sicherheitsnetz: jeder Pfad muss innerhalb von ./workdir liegen und darf
    nicht workdir selbst, final/ oder logs/ sein. Ohne diese Pruefung wuerde
    ein falsch gesetzter Konfigurationswert (oder ein Symlink) mit
    shutil.rmtree beliebige Verzeichnisse loeschen.

    dry_run zeigt nur, was geloescht wuerde. Ein Trockenlauf, der trotzdem
    loescht, waere kein Trockenlauf - 2.0 hatte diese Luecke.
    """
    logger.info("=== Schritt 5: CLEANUP gestartet ===")
    ui.step_header(5, "CLEANUP", "Loescht temporaere Arbeitsordner")
    stats = CleanupStats()
    start = datetime.now()
    geschuetzt = {
        str(Path(WORKDIR).resolve()),
        str(Path(FINAL_DIR).resolve()),
        str(Path(LOG_DIR).resolve()),
    }

    for pfad, beschreibung in config.cleanup.folders_to_delete:
        if not os.path.exists(pfad):
            ui.detail(f"Ordner '{pfad}' existiert nicht - uebersprungen")
            stats.skipped.append(pfad)
            continue

        ziel = str(Path(pfad).resolve())
        if ziel in geschuetzt or not _ist_im_workdir(pfad):
            ui.error(
                f"'{pfad}' liegt nicht innerhalb von {WORKDIR} oder ist geschuetzt - "
                f"wird nicht geloescht."
            )
            stats.kept.append(pfad)
            continue

        groesse_gb = berechne_ordner_groesse_gb(pfad)
        ui.tabelle(
            beschreibung,
            ["Eigenschaft", "Wert"],
            [["Pfad", pfad], ["Groesse", f"{groesse_gb:.2f} GB"]],
            farbe_rand=GELB,
            kopf_farbe=GELB,
        )

        if dry_run:
            ui.detail(f"Dry-Run: '{pfad}' wuerde geloescht ({groesse_gb:.2f} GB)")
            stats.kept.append(pfad)
            continue

        loeschen = True if force else ui.confirm(f"Ordner '{pfad}' wirklich loeschen?", default=True)
        if not loeschen:
            ui.detail(f"Ordner '{pfad}' wird behalten")
            stats.kept.append(pfad)
            continue

        try:
            shutil.rmtree(pfad)
            ui.success(f"Ordner '{pfad}' geloescht ({groesse_gb:.2f} GB freigegeben)")
            stats.deleted.append(pfad)
            stats.bytes_freed += int(groesse_gb * (1024**3))
        except Exception as fehler:
            ui.error(f"Fehler beim Loeschen von '{pfad}': {fehler}")
            logger.exception(f"Cleanup-Fehler: {pfad}")

    zeilen = [
        ["Geloeschte Ordner", str(len(stats.deleted))],
        ["Behaltene Ordner", str(len(stats.kept))],
        ["Uebersprungen", str(len(stats.skipped))],
    ]
    if stats.gb_freed > 0:
        zeilen.append(["Freigegeben", f"{stats.gb_freed:.2f} GB"])
    ui.tabelle(
        "Cleanup abgeschlossen - Statistik",
        ["Metrik", "Wert"],
        zeilen,
        farbe_rand=GELB,
        ausrichtung=["links", "rechts"],
        kopf_farbe=GELB,
    )

    details = [f"{len(stats.deleted)} geloescht"]
    if stats.kept:
        details.append(f"{len(stats.kept)} behalten")
    if stats.gb_freed > 0:
        details.append(f"{stats.gb_freed:.2f} GB freigegeben")
    if dry_run:
        details = [f"{len(stats.kept)} wuerden geloescht", "Simulation"]
    return StepResult(
        number=5,
        name="Cleanup",
        status=StepStatus.DRY_RUN if dry_run else StepStatus.OK,
        duration_sec=(datetime.now() - start).total_seconds(),
        detail=", ".join(details),
    )

# ============================================================================
# === 6. CLI & MAIN ==========================================================
# ============================================================================
#
# 2.0 nutzte Typer. Typer bringt Klick und damit eine Abhaengigkeit mit, die
# fuer sieben Subcommands nicht noetig ist. argparse kann dasselbe; Farbe
# kommt aus der Hilfe-Nachbearbeitung weiter unten.

BEFEHLE = [
    ("run", "Kompletten Workflow ausfuehren (Schritte 1-5)"),
    ("mirror", "Schritt 1: FTP Mirror"),
    ("sort", "Schritt 2: Sort"),
    ("compare", "Schritt 3: Compare"),
    ("finalize", "Schritt 4: Finalize"),
    ("cleanup", "Schritt 5: Cleanup"),
    ("info", "Aktuelle Konfiguration anzeigen"),
]


def _faerbe_hilfe(text: str) -> str:
    """
    Faerbt die argparse-Hilfe nach.

    argparse kennt keine Styles. Die Alternative waere ein eigener
    HelpFormatter mit ueberschriebenen Methoden; das ist deutlich mehr Code
    und bricht bei jeder Python-Aenderung an den internen APIs. Das
    Nachfaerben des fertigen Textes ist robust, weil es nur Muster trifft,
    die argparse garantiert erzeugt.
    """
    if not FARBEN_AKTIV:
        return text

    text = re.sub(r"^usage:", farbe("usage:", CYAN, fett=True), text, flags=re.M)
    text = re.sub(
        r"^(options|optional arguments|Befehle|positional arguments):",
        lambda m: farbe(m.group(1) + ":", GELB, fett=True),
        text,
        flags=re.M,
    )
    # Optionsnamen: -n, --dry-run, --ftp-host HOST
    text = re.sub(
        r"(?<![\w-])(-[A-Za-z]|--[A-Za-z][\w-]*)",
        lambda m: farbe(m.group(1), CYAN),
        text,
    )
    # Progname in der usage-Zeile
    text = re.sub(
        r"(usage:\s*)(\S+)",
        lambda m: m.group(1) + farbe(m.group(2), WEISS, fett=True),
        text,
    )
    return text


class FarbigerParser(argparse.ArgumentParser):
    """ArgumentParser mit farbiger Hilfe und farbiger Fehlermeldung."""

    def print_help(self, file=None) -> None:
        print(_faerbe_hilfe(self.format_help()), file=file or sys.stdout)

    def error(self, message: str) -> None:
        aufruf = f"{self.prog}: Fehler: {message}"
        print(farbe(aufruf, ROT, fett=True), file=sys.stderr)
        print(_faerbe_hilfe(self.format_usage()), file=sys.stderr, end="")
        raise SystemExit(2)


def _basis_parser() -> FarbigerParser:
    """Gemeinsame Optionen, die vor und nach dem Subcommand gelten."""
    parser = FarbigerParser(add_help=False)
    parser.add_argument(
        "--no-color",
        action="store_true",
        help="Ausgabe ohne ANSI-Farben (auch via NO_COLOR)",
    )
    return parser


def baue_parser() -> FarbigerParser:
    """
    Baut den kompletten CLI-Baum.

    Die Optionen entsprechen 1:1 denen von 2.0, damit bestehende Aufrufe
    weiter funktionieren:
        run --dry-run --no-confirm --step 3 --verbose --ftp-host ... --remote-path ...
    """
    basis = _basis_parser()
    epilog = "Befehle:\n" + "\n".join(f"  {name:<10} {text}" for name, text in BEFEHLE)
    epilog += "\n\nBeispiel:\n  python3 whdload_workflow.py run --step 2"

    parser = FarbigerParser(
        prog="whdload_workflow.py",
        description=(
            f"{PROGRAMM} {VERSION} - Mirror, Sort, Compare, Finalize und Cleanup "
            "fuer WHDLoad-Archive. Benoetigt nur die Python-Standardbibliothek."
        ),
        epilog=epilog,
        formatter_class=argparse.RawDescriptionHelpFormatter,
        parents=[basis],
    )
    parser.add_argument("--version", action="store_true", help="Version anzeigen")

    sub = parser.add_subparsers(dest="befehl", metavar="BEFEHL")

    def unterbefehl(name: str, hilfe: str, beschreibung: str) -> FarbigerParser:
        return sub.add_parser(
            name,
            help=hilfe,
            description=beschreibung,
            formatter_class=argparse.RawDescriptionHelpFormatter,
            parents=[basis],
        )

    p_run = unterbefehl("run", "Kompletter Workflow (Schritte 1-5)", "Fuehrt alle fuenf Schritte nacheinander aus.")
    p_run.add_argument("-n", "--dry-run", action="store_true", help="Simulation ohne Dateiaktion")
    p_run.add_argument("--no-confirm", action="store_true", help="Keine interaktiven Rueckfragen")
    p_run.add_argument("-s", "--step", type=int, choices=range(1, 6), metavar="N", help="Nur Schritte 1..N ausfuehren")
    p_run.add_argument("-v", "--verbose", action="store_true", help="Erweiterte Logs mit Stacktraces")
    p_run.add_argument("--ftp-host", help="FTP-Server ueberschreiben")
    p_run.add_argument("--remote-path", help="Remote-Pfad ueberschreiben")
    p_run.set_defaults(func=cmd_run)

    p_mirror = unterbefehl("mirror", "Schritt 1: FTP Mirror", "Laedt die Archive vom FTP-Server nach workdir/mirror/.")
    p_mirror.add_argument("-n", "--dry-run", action="store_true", help="Simulation ohne Download")
    p_mirror.add_argument("--ftp-host", help="FTP-Server")
    p_mirror.add_argument("--remote-path", help="Remote-Pfad")
    p_mirror.add_argument("-v", "--verbose", action="store_true", help="Erweiterte Logs")
    p_mirror.set_defaults(func=cmd_mirror)

    p_sort = unterbefehl("sort", "Schritt 2: Sort", "Sortiert die Archive nach Amiga-System nach workdir/sort/.")
    p_sort.add_argument("-s", "--source", help="Quellordner")
    p_sort.add_argument("-t", "--target", help="Zielordner")
    p_sort.add_argument("-n", "--dry-run", action="store_true", help="Nur Analyse, nichts kopieren")
    p_sort.add_argument("-v", "--verbose", action="store_true", help="Erweiterte Logs")
    p_sort.set_defaults(func=cmd_sort)

    p_compare = unterbefehl("compare", "Schritt 3: Compare", "Vergleicht zwei Verzeichnisse auf Dateinamen-Ebene.")
    p_compare.add_argument("--dir1", help="Erstes Verzeichnis")
    p_compare.add_argument("--dir2", help="Zweites Verzeichnis")
    p_compare.add_argument("-v", "--verbose", action="store_true", help="Erweiterte Logs")
    p_compare.set_defaults(func=cmd_compare)

    p_final = unterbefehl("finalize", "Schritt 4: Finalize", "Waehlt je Spiel die beste Sprachversion und kopiert sie nach workdir/final/.")
    p_final.add_argument("-n", "--dry-run", action="store_true", help="Simulation ohne Kopieren")
    p_final.add_argument("-s", "--source", help="Quellordner")
    p_final.add_argument("-t", "--target", help="Zielordner")
    p_final.add_argument("-v", "--verbose", action="store_true", help="Erweiterte Logs")
    p_final.set_defaults(func=cmd_finalize)

    p_clean = unterbefehl("cleanup", "Schritt 5: Cleanup", "Loescht workdir/mirror/ und workdir/sort/.")
    p_clean.add_argument("-f", "--force", action="store_true", help="Keine Bestaetigung pro Ordner")
    p_clean.add_argument("-n", "--dry-run", action="store_true", help="Nur zeigen, was geloescht wuerde")
    p_clean.add_argument("-v", "--verbose", action="store_true", help="Erweiterte Logs")
    p_clean.set_defaults(func=cmd_cleanup)

    p_info = unterbefehl("info", "Konfiguration anzeigen", "Zeigt die aktuelle Konfiguration als Tabelle.")
    p_info.add_argument("-v", "--verbose", action="store_true", help="Erweiterte Logs")
    p_info.set_defaults(func=cmd_info)

    return parser


def _true_color_faehig() -> bool:
    """True, wenn das Terminal 24-Bit-Farben ankuendigt."""
    wert = os.environ.get("COLORTERM", "").lower()
    return "truecolor" in wert or "24bit" in wert


def _farben_erlaubt(no_color_flag: bool = False) -> bool:
    """
    Zentrale Entscheidung ueber Farbe.

    NO_COLOR ist eine Konvention (no-color.org), kein Vorschlag: ist die
    Variable gesetzt, wird nicht gefaerbt, egal was sonst gilt.
    """
    if os.environ.get("NO_COLOR"):
        return False
    if no_color_flag:
        return False
    return sys.stdout.isatty()


def _farbmodus(args: argparse.Namespace) -> None:
    """Wendet den Farbmodus nach dem Parsen an (beruecksichtigt --no-color)."""
    farbmodus_setzen(
        _farben_erlaubt(getattr(args, "no_color", False)), _true_color_faehig()
    )


def _vorbereiten(config: Config, args: argparse.Namespace) -> tuple[UI, str]:
    """Logging und UI fuer einen Befehl aufsetzen (gleiche Reihenfolge wie 2.0)."""
    log_datei = setup_logging(config, debug=getattr(args, "verbose", False))
    ui = UI(config)
    return ui, log_datei


def _fuehre_aus(funktion, config: Config, ui: UI, name: str, **kwargs) -> StepResult:
    """
    Fuehrt einen Schritt aus und wandelt Fehler in ein StepResult.

    Erwartete Fachfehler erscheinen als Fehlerpanel, unerwartete zusaetzlich
    mit Stacktrace im Log. Ein einzelner Fehler beendet den Workflow nicht,
    solange der Benutzer weitermachen will.
    """
    logger.info(f"Starte Schritt: {name} (kwargs: {kwargs})")
    try:
        ergebnis = funktion(config, ui, **kwargs)
        return ergebnis[0] if isinstance(ergebnis, tuple) else ergebnis
    except WHDLoadError as fehler:
        ui.error(str(fehler), f"{name}-Fehler")
        logger.exception(f"{name}-Fehler")
        return StepResult(0, name, StepStatus.ERROR, detail=str(fehler))
    except Exception as fehler:
        ui.error(str(fehler), f"Unerwarteter Fehler in {name}")
        logger.exception(f"Unerwarteter Fehler in {name}")
        return StepResult(0, name, StepStatus.ERROR, detail=str(fehler))


def cmd_run(args: argparse.Namespace) -> int:
    """Kompletter Workflow mit Rueckfragen zwischen den Schritten."""
    config = Config()
    if args.ftp_host:
        config.ftp.host = args.ftp_host
    if args.remote_path:
        config.ftp.remote_path = args.remote_path

    ui, log_datei = _vorbereiten(config, args)
    logger.info(
        f"=== WORKFLOW GESTARTET (dry_run={args.dry_run}, max_step={args.step or 5}) ==="
    )
    ui.banner()
    ui.detail(f"Log-Datei: {log_datei}")
    ui.detail(f"Gestartet: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    ui.detail(f"Dry-Run: {'Ja' if args.dry_run else 'Nein'}")

    max_schritt = args.step or 5
    ergebnisse: list[StepResult] = []
    abbruch = False

    def frage(text: str) -> bool:
        return True if args.no_confirm else ui.confirm(text, default=True)

    if max_schritt >= 1:
        if frage("Schritt 1 (FTP Mirror) ausfuehren?"):
            r = _fuehre_aus(run_mirror, config, ui, "FTP Mirror", dry_run=args.dry_run)
            ergebnisse.append(r)
            if r.status == StepStatus.ERROR and not args.no_confirm:
                if not ui.confirm("Trotzdem fortfahren?", default=False):
                    abbruch = True
        else:
            ergebnisse.append(StepResult(1, "FTP Mirror", StepStatus.SKIPPED, detail="uebersprungen"))

    if not abbruch and max_schritt >= 2:
        if frage("Schritt 2 (Sort) ausfuehren?"):
            r = _fuehre_aus(run_sort, config, ui, "Sort", dry_run=args.dry_run)
            ergebnisse.append(r)
            if r.status == StepStatus.ERROR and not args.no_confirm:
                if not ui.confirm("Trotzdem fortfahren?", default=False):
                    abbruch = True
        else:
            ergebnisse.append(StepResult(2, "Sort", StepStatus.SKIPPED, detail="uebersprungen"))

    # Im Trockenlauf entstehen keine sortierten Archive. Compare und Finalize
    # haetten also keine Quelle; sie als Fehler zu melden, wuerde einen
    # korrekt verlaufenen Trockenlauf als gescheitert darstellen.
    dry_run_ohne_daten = args.dry_run and not os.path.isdir(config.sort.target_dir)

    if not abbruch and max_schritt >= 3:
        if dry_run_ohne_daten:
            ergebnisse.append(
                StepResult(
                    3,
                    "Compare",
                    StepStatus.DRY_RUN,
                    detail="uebersprungen - Dry-Run erzeugt keine sortierten Archive",
                )
            )
        elif frage("Schritt 3 (Compare) ausfuehren?"):
            r = _fuehre_aus(run_compare, config, ui, "Compare")
            ergebnisse.append(r)
            if r.status == StepStatus.ERROR and not args.no_confirm:
                if not ui.confirm("Trotzdem fortfahren?", default=False):
                    abbruch = True
        else:
            ergebnisse.append(StepResult(3, "Compare", StepStatus.SKIPPED, detail="uebersprungen"))

    if not abbruch and max_schritt >= 4:
        if dry_run_ohne_daten:
            ergebnisse.append(
                StepResult(
                    4,
                    "Finalize",
                    StepStatus.DRY_RUN,
                    detail="uebersprungen - Dry-Run erzeugt keine sortierten Archive",
                )
            )
        elif frage("Schritt 4 (Finalize) ausfuehren?"):
            # Dry-Run gewinnt immer: auch wenn der Benutzer "kopieren" sagt,
            # bleibt --dry-run eine Simulation.
            simulieren = args.dry_run
            if not args.no_confirm and not args.dry_run:
                simulieren = not ui.confirm(
                    "Dateien wirklich kopieren? (Nein = nur Simulation)", default=True
                )
            r = _fuehre_aus(run_finalize, config, ui, "Finalize", dry_run=simulieren)
            ergebnisse.append(r)
        else:
            ergebnisse.append(StepResult(4, "Finalize", StepStatus.SKIPPED, detail="uebersprungen"))

    if not abbruch and max_schritt >= 5:
        if frage("Schritt 5 (Cleanup) ausfuehren?"):
            r = _fuehre_aus(
                run_cleanup,
                config,
                ui,
                "Cleanup",
                force=args.no_confirm,
                dry_run=args.dry_run,
            )
            ergebnisse.append(r)
        else:
            ergebnisse.append(StepResult(5, "Cleanup", StepStatus.SKIPPED, detail="uebersprungen"))

    logger.info("=== WORKFLOW ABGESCHLOSSEN ===")
    ui.rule("WORKFLOW ABGESCHLOSSEN", farbe_rand=HELLGRUEN, fett=True)
    ui.detail(f"Beendet: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    ui.summary_table(ergebnisse, log_datei)
    return 0 if all(r.status != StepStatus.ERROR for r in ergebnisse) else 1


def cmd_mirror(args: argparse.Namespace) -> int:
    """Einzelschritt 1."""
    config = Config()
    if args.ftp_host:
        config.ftp.host = args.ftp_host
    if args.remote_path:
        config.ftp.remote_path = args.remote_path
    ui, log_datei = _vorbereiten(config, args)
    ui.banner()
    ergebnis, _stats = run_mirror(config, ui, dry_run=args.dry_run)
    ui.summary_table([ergebnis], log_datei)
    return 0 if ergebnis.status != StepStatus.ERROR else 1


def cmd_sort(args: argparse.Namespace) -> int:
    """Einzelschritt 2."""
    config = Config()
    if args.source:
        config.sort.source_dir = args.source
        config.finalize.source_dir = args.source
    if args.target:
        config.sort.target_dir = args.target
    ui, log_datei = _vorbereiten(config, args)
    ui.banner()
    ergebnis = run_sort(config, ui, dry_run=args.dry_run)
    ui.summary_table([ergebnis], log_datei)
    return 0 if ergebnis.status != StepStatus.ERROR else 1


def cmd_compare(args: argparse.Namespace) -> int:
    """Einzelschritt 3."""
    config = Config()
    ui, log_datei = _vorbereiten(config, args)
    ui.banner()
    ergebnis = run_compare(config, ui, dir1=args.dir1, dir2=args.dir2)
    ui.summary_table([ergebnis], log_datei)
    return 0 if ergebnis.status != StepStatus.ERROR else 1


def cmd_finalize(args: argparse.Namespace) -> int:
    """Einzelschritt 4."""
    config = Config()
    if args.source:
        config.finalize.source_dir = args.source
    if args.target:
        config.finalize.target_dir = args.target
    ui, log_datei = _vorbereiten(config, args)
    ui.banner()
    ergebnis = run_finalize(config, ui, dry_run=args.dry_run)
    ui.summary_table([ergebnis], log_datei)
    return 0 if ergebnis.status != StepStatus.ERROR else 1


def cmd_cleanup(args: argparse.Namespace) -> int:
    """Einzelschritt 5."""
    config = Config()
    ui, log_datei = _vorbereiten(config, args)
    ui.banner()
    ergebnis = run_cleanup(config, ui, force=args.force, dry_run=args.dry_run)
    ui.summary_table([ergebnis], log_datei)
    return 0


def cmd_info(args: argparse.Namespace) -> int:
    """Konfigurationsuebersicht ohne Dateiaktion."""
    config = Config()
    ui, _log_datei = _vorbereiten(config, args)
    ui.banner()

    # Gruppen statt einer langen Liste: die Trennlinien markieren die
    # Bereiche, und der Bereichsname steht nur einmal je Gruppe. Das ist
    # die gleiche Gliederung wie in der 2.0-Tabelle.
    gruppen: list[tuple[str, list[tuple[str, str]]]] = [
        (
            "FTP",
            [
                ("Host", config.ftp.host),
                ("Port", str(config.ftp.port)),
                ("Remote-Pfad", config.ftp.remote_path),
                ("Mirror-Ordner", config.mirror_folder),
                ("Passiv", "Ja" if config.ftp.use_passive else "Nein"),
                ("Hash-Verify", config.ftp.verify_hash),
            ],
        ),
        (
            "Sort",
            [
                ("Quelle", config.sort.source_dir),
                ("Ziel", config.sort.target_dir),
            ],
        ),
        (
            "Compare",
            [
                ("Default Dir 1", config.compare.default_dir1),
                ("Default Dir 2", config.compare.default_dir2),
            ],
        ),
        (
            "Finalize",
            [
                ("Quelle", config.finalize.source_dir),
                ("Ziel", config.finalize.target_dir),
                ("Plattformen", ", ".join(config.finalize.platform_folders)),
                ("Sprach-Prioritaet", " > ".join(config.finalize.language_priority)),
            ],
        ),
        (
            "Allgemein",
            [
                ("Log-Ordner", config.general.log_dir),
                ("Archiv-Endung", config.general.archive_extension),
            ],
        ),
    ]

    zeilen: list[list[str]] = []
    trennungen: list[int] = []
    for bereich, eintraege in gruppen:
        if zeilen:
            trennungen.append(len(zeilen))
        for position, (label, wert) in enumerate(eintraege):
            zeilen.append([bereich if position == 0 else "", label, wert])

    ui.tabelle(
        "Aktuelle Konfiguration",
        ["Bereich", "Einstellung", "Wert"],
        zeilen,
        farbe_rand=CYAN,
        kopf_farbe=CYAN,
        trennungen=trennungen,
    )
    return 0


def main(argv: list[str] | None = None) -> int:
    """
    Einstiegspunkt.

    Der Farbmodus muss VOR parse_args gesetzt werden: --help beendet den
    Parser sofort, und eine Hilfe, die erst nachtraeglich eingefaerbt wird,
    erscheint sonst immer bunt - auch in einer Pipe. --no-color wird dafuer
    vorab aus argv gelesen, weil das Attribut zu diesem Zeitpunkt noch
    nicht existiert.
    """
    roh_argv = list(sys.argv[1:] if argv is None else argv)
    farbmodus_setzen(
        _farben_erlaubt(no_color_flag="--no-color" in roh_argv),
        _true_color_faehig(),
    )

    parser = baue_parser()
    args = parser.parse_args(roh_argv)
    _farbmodus(args)

    if getattr(args, "version", False):
        print(f"{PROGRAMM} v{VERSION}")
        return 0

    if not getattr(args, "befehl", None):
        parser.print_help()
        return 0

    return args.func(args)


def _einstieg() -> None:
    """Faengt Strg+C und unerwartete Fehler an der obersten Ebene ab."""
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        print(f"\n{farbe('Programm durch Benutzer abgebrochen (Strg+C)', GRAU, dunkel=True)}")
        sys.exit(130)
    except BrokenPipeError:
        # Tritt bei "| head" auf; ohne diesen Zweig meldet Python einen
        # Traceback beim Schliessen von stdout.
        try:
            sys.stdout.close()
        except Exception:
            pass
        sys.exit(0)
    except Exception as fehler:
        logger.exception("Unerwarteter Fehler an der obersten Ebene")
        print(f"\n{farbe('✗ Unerwarteter Fehler: ' + str(fehler), ROT, fett=True)}")
        print(farbe("Verwende --verbose fuer Details im Log.", GRAU, dunkel=True))
        sys.exit(1)


if __name__ == "__main__":
    _einstieg()
