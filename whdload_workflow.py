#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
================================================================================
WHDLoad Workflow
Kompletter Workflow für WHDLoad-Archive: Mirror, Sort, Compare & Finalize
================================================================================

Programm: whdload_workflow.py
Version: 1.3
Datum: 2025-11-01

Beschreibung:
Dieses Skript vereint 5 separate Workflow-Schritte in einem einzigen Programm:
1. FTP Mirror: Lädt WHDLoad-Archive von FTP-Server herunter
2. Sort: Sortiert Archive nach Amiga-Systemtyp (500/1200/CD32/CDTV)
3. Compare: Vergleicht Verzeichnisse und zeigt Unterschiede
4. Finalize: Wählt beste Sprachversionen aus und erstellt finale Struktur
5. Cleanup: Löscht temporäre Arbeitsordner (whdload-mirror, whdload-sort)

Zwischen jedem Schritt wird der Benutzer gefragt, ob er fortfahren möchte.

Verwendung:
    python whdload_workflow.py

Alle Einstellungen können im KONFIGURATIONSBEREICH angepasst werden.
================================================================================
"""

# ============================================================================
# IMPORTS - Erforderliche Python-Bibliotheken
# ============================================================================
import os                      # Dateisystem-Operationen
import sys                     # System-Parameter und Streams
import ftplib                  # FTP-Client für Downloads
import time                    # Zeit-Funktionen (Sleep, Timestamps)
import zlib                    # CRC32-Berechnung
import hashlib                 # Hash-Funktionen (MD5, SHA)
import socket                  # Socket-Operationen für FTP
import re                      # Reguläre Ausdrücke
import shutil                  # Datei-Operationen (Kopieren)
from pathlib import Path       # Moderne Pfad-Verwaltung
from collections import defaultdict  # Automatische Dictionary-Initialisierung
from datetime import datetime, timezone  # Zeitstempel und Zeitzonen
from posixpath import join as pjoin, normpath  # POSIX-Pfad-Funktionen für FTP

# ============================================================================
# KONFIGURATIONSBEREICH - Hier alle Einstellungen anpassen
# ============================================================================

# --- SCHRITT 1: FTP MIRROR KONFIGURATION ---
# Einstellungen für den Download von WHDLoad-Archiven

# FTP-Server Verbindungsdetails
FTP_HOST = "ftp2.grandis.nu"                          # FTP-Server-Adresse
FTP_PORT = 21                                          # FTP-Port (Standard: 21)
FTP_USER = "ftp"                                       # Benutzername (anonym)
FTP_PASS = "any"                                       # Passwort (anonym)
REMOTE_PATH = "/Retroplay WHDLoad Packs/Commodore_Amiga_-_WHDLoad_-_Games/"  # Remote-Startpfad

# Lokales Zielverzeichnis für den Mirror
MIRROR_ORDNER = "whdload-mirror"                       # Lokaler Zielordner für Downloads

# FTP-Verbindungseinstellungen
USE_PASSIVE = True                                     # Passivmodus (empfohlen hinter Firewalls)
TIMEOUT = 300                                          # Timeout in Sekunden
MAX_RETRIES = 3                                        # Maximale Wiederholungsversuche bei Fehlern
SLEEP_BETWEEN_RETRIES = 2.0                           # Wartezeit zwischen Versuchen (Sekunden)

# Transfer-Einstellungen
BLOCKSIZE = 1024 * 1024                               # Download-Blockgröße (1 MiB)
RESUME_PARTIAL = True                                  # Resume bei abgebrochenen Downloads
SKIP_EXISTING_SAME_SIZE = True                        # Dateien gleicher Größe überspringen
OVERWRITE_DIFFER = False                              # Dateien mit abweichender Größe überschreiben
SET_MTIME = True                                      # Zeitstempel vom Server übernehmen

# Hash-Verifikation (Integritätsprüfung)
VERIFY_HASH = "auto"                                  # auto, true oder false
PREFERRED_HASH_ALGOS = ["CRC32", "MD5", "SHA-1", "SHA-256"]  # Bevorzugte Algorithmen

# Anzeige-Einstellungen für Pfade (nur Ausgabe, keine Auswirkung auf echte Pfade)
DISPLAY_PATH_REWRITE_ENABLED = True                   # Pfad-Umschreibung für Anzeige aktivieren
DISPLAY_PATH_REWRITE_FROM = "/Retroplay WHDLoad Packs/Commodore_Amiga_-_WHDLoad_-_Games/"
DISPLAY_PATH_REWRITE_TO = "/Retroplay WHDLoad.../... "
DISPLAY_PATH_MAXLEN = 0                               # Max. Länge für Anzeige (0 = keine Begrenzung)
DISPLAY_PATH_ELLIPSIS = "middle"                      # Position der Kürzung: "left", "middle", "right"

# --- SCHRITT 2: SORT KONFIGURATION ---
# Einstellungen für die Sortierung nach Amiga-Systemtyp

SORT_QUELLORDNER = "whdload-mirror"                   # Quellordner (=Ausgabe von Schritt 1)
SORT_ZIELORDNER = "whdload-sort"                      # Zielordner für sortierte Dateien

# Unterordner-Namen für verschiedene Amiga-Systeme
ORDNER_AMIGA1200 = "amiga1200"                        # Amiga 1200/4000 (AGA-Chipset)
ORDNER_AMIGACD32 = "amigacd32"                        # Amiga CD32
ORDNER_AMIGACDTV = "amigacdtv"                        # Amiga CDTV
ORDNER_AMIGA500 = "amiga500"                          # Amiga 500/600 (OCS/ECS-Chipset)

# --- SCHRITT 3: COMPARE KONFIGURATION ---
# Standard-Ordner für Vergleich (können vom Benutzer geändert werden)
COMPARE_ORDNER1_DEFAULT = "whdload-mirror"  # Standard-Quellordner
COMPARE_ORDNER2_DEFAULT = "whdload-sort"     # Standard-Zielordner
COMPARE_AUSGABE_BREITE = 70                           # Breite der Trennlinien in der Ausgabe

# --- SCHRITT 4: FINALIZE KONFIGURATION ---
# Einstellungen für die finale Sprachauswahl

FINAL_QUELLORDNER = "whdload-sort"                    # Quellordner (=Ausgabe von Schritt 2)
FINAL_ZIELORDNER = "whdload-final"                    # Finaler Zielordner

# Plattform-Unterordner die verarbeitet werden sollen
PLATTFORM_ORDNER = [
    "amiga1200",                                       # Amiga 1200 (AGA)
    "amigacd32",                                       # Amiga CD32
    "amigacdtv",                                       # Amiga CDTV
    "amiga500"                                         # Amiga 500 (OCS/ECS)
]

# Standard-Sprache wenn keine erkannt wird
STANDARD_SPRACHE = "En"                               # Standard-Sprache wenn keine erkannt wird

# Sprach-Prioritäten (erste gefundene Sprache wird bevorzugt)
SPRACH_PRIORITAET = [
    "De",                                              # Deutsch (höchste Priorität)
    "En",                                              # Englisch
    "Fr"                                               # Französisch (niedrigste Priorität)
]

# Dateinamen-Optionen
UNTERSTRICHE_DURCH_LEERZEICHEN = True                 # True: "_" wird zu " " im Dateinamen

# Anzeige-Limits
MAX_ANGEZEIGTE_MEHRFACH_SPIELE = 15                   # Max. Anzahl detailliert angezeigter Multi-Versionen
MAX_SPIELE_PRO_SPRACHE = 20                           # Max. Spiele pro Sprache in Statistik-Datei

# Dateinamen-Parsing-Patterns
ARCHIV_ENDUNG = ".lha"                                # Dateiendung für WHDLoad-Archive
ZAHLENCODE_PATTERN = r'_(\d{4}(?:&\d{4})*)$'         # Pattern für 4-stellige Codes
SPRACHCODE_PATTERN = r'^[A-Z][a-z](?:[A-Z][a-z])*$'  # Pattern für Ländercodes

# --- ALLGEMEINE EINSTELLUNGEN ---
# Einstellungen die für alle Schritte gelten

# Log-Verzeichnis (wird im Skript-Verzeichnis erstellt)
LOG_ORDNER = "logs"                                   # Name des Log-Unterordners

# Encoding für alle Log-Dateien
LOG_ENCODING = "utf-8"                                # Zeichenkodierung

# Separator für Ausgaben
SEPARATOR_LAENGE = 80                                 # Länge der Trennlinien

# ============================================================================
# ENDE KONFIGURATIONSBEREICH
# ============================================================================

# ============================================================================
# GLOBALE VARIABLEN - Laufzeit-Status
# ============================================================================

# FTP Mirror Statistiken
gesamt_bytes_geladen = 0                              # Summe übertragener Bytes
gesamt_sekunden_transfer = 0.0                        # Summe Transferzeiten
gesamt_dateien_sync = 0                               # Anzahl verarbeiteter Dateien
gesamt_ordner_sync = 0                                # Anzahl besuchter Ordner

# FTP Server-Fähigkeiten
HASH_UNTERSTUETZT = False                             # Unterstützt Server HASH?
HASH_ALGO = None                                      # Gewählter HASH-Algorithmus
XCRC_UNTERSTUETZT = False                             # Unterstützt Server XCRC?

# Aktueller Zeitstempel für Logs (wird einmalig beim Start gesetzt)
AKTUELLER_ZEITSTEMPEL = datetime.now().strftime("%Y-%m-%d_%H-%M-%S")

# ============================================================================
# LOGGER-KLASSE - Ausgabe auf Konsole UND in Log-Datei
# ============================================================================

class DoppelLogger:
    """
    Logger-Klasse für simultane Ausgabe auf Bildschirm und Log-Datei.
    
    Alle print()-Ausgaben werden automatisch sowohl auf der Konsole angezeigt
    als auch in eine Log-Datei geschrieben. Dies ermöglicht spätere Analyse
    und Dokumentation des Programmablaufs.
    
    Verwendung:
        logger = DoppelLogger("mein_log.log")
        sys.stdout = logger
        print("Diese Nachricht erscheint auf Konsole UND in der Datei")
        logger.close()
    """
    
    def __init__(self, logdatei_pfad):
        """
        Initialisiert den Logger.
        
        Args:
            logdatei_pfad (str): Pfad zur Log-Datei
        """
        self.terminal = sys.stdout                     # Originale Konsolen-Ausgabe speichern
        self.logdatei = open(logdatei_pfad, 'a', encoding=LOG_ENCODING)  # Log-Datei öffnen
        
    def write(self, nachricht):
        """
        Schreibt Nachricht auf Konsole und in Log-Datei.
        
        Args:
            nachricht (str): Auszugebende Nachricht
        """
        self.terminal.write(nachricht)                 # Ausgabe auf Bildschirm
        self.terminal.flush()                          # Sofort anzeigen
        self.logdatei.write(nachricht)                 # Ausgabe in Datei
        self.logdatei.flush()                          # Sofort schreiben
        
    def flush(self):
        """
        Leert die Ausgabe-Puffer.
        
        Wird von Python automatisch aufgerufen um sicherzustellen,
        dass alle Daten geschrieben wurden.
        """
        self.terminal.flush()
        self.logdatei.flush()
        
    def close(self):
        """
        Schließt die Log-Datei ordnungsgemäß.
        
        Sollte immer am Ende des Programms aufgerufen werden.
        """
        self.logdatei.close()

# ============================================================================
# HILFSFUNKTIONEN - Allgemeine Utility-Funktionen
# ============================================================================

def erstelle_log_ordner():
    """
    Erstellt den Log-Ordner falls er nicht existiert.
    
    Der Log-Ordner wird im gleichen Verzeichnis wie das Skript erstellt.
    
    Returns:
        Path: Pfad zum Log-Ordner
    """
    skript_ordner = Path(__file__).parent              # Verzeichnis des Skripts
    log_pfad = skript_ordner / LOG_ORDNER             # Vollständiger Pfad zum Log-Ordner
    log_pfad.mkdir(exist_ok=True)                     # Erstelle Ordner (kein Fehler wenn vorhanden)
    return log_pfad

def erstelle_logdatei_namen(basis_name):
    """
    Erstellt einen Log-Dateinamen mit aktuellem Datum.
    
    Format: YYYY-MM-DD_HH-MM-SS-basisname.log
    
    Args:
        basis_name (str): Basis-Name für die Log-Datei
        
    Returns:
        str: Vollständiger Pfad zur Log-Datei
    """
    log_ordner = erstelle_log_ordner()                # Stelle sicher dass Log-Ordner existiert
    dateiname = f"{AKTUELLER_ZEITSTEMPEL}-{basis_name}.log"
    return str(log_ordner / dateiname)

def benutzer_frage(frage_text, standard="j"):
    """
    Stellt dem Benutzer eine Ja/Nein-Frage.
    
    Akzeptiert verschiedene Eingaben:
    - Ja: j, ja, y, yes (Groß-/Kleinschreibung egal)
    - Nein: n, nein, no
    - Leer: Verwendet Standard-Antwort
    
    Args:
        frage_text (str): Die zu stellende Frage
        standard (str): Standard-Antwort bei leerer Eingabe ("j" oder "n")
        
    Returns:
        bool: True für Ja, False für Nein
    """
    # Zeige Frage mit Standard-Antwort
    standard_text = "J/n" if standard.lower() == "j" else "j/N"
    eingabe = input(f"\n{frage_text} [{standard_text}]: ").strip().lower()
    
    # Leere Eingabe = Standard verwenden
    if not eingabe:
        eingabe = standard.lower()
    
    # Prüfe ob Antwort positiv ist
    return eingabe in ['j', 'ja', 'y', 'yes']

def zeige_schritt_header(schritt_nummer, schritt_name, beschreibung):
    """
    Zeigt einen formatierten Header für einen Workflow-Schritt.
    
    Args:
        schritt_nummer (int): Nummer des Schritts (1-4)
        schritt_name (str): Name des Schritts
        beschreibung (str): Kurze Beschreibung was der Schritt macht
    """
    print("\n" + "=" * SEPARATOR_LAENGE)
    print(f"SCHRITT {schritt_nummer}: {schritt_name}")
    print("=" * SEPARATOR_LAENGE)
    print(f"{beschreibung}")
    print("=" * SEPARATOR_LAENGE + "\n")

def formatiere_dategroesse(bytes_anzahl):
    """
    Formatiert eine Bytezahl in menschenlesbares Format.
    
    Args:
        bytes_anzahl (int): Anzahl der Bytes
        
    Returns:
        str: Formatierte Größenangabe (z.B. "1.5 GB", "234.7 MB")
    """
    if bytes_anzahl < 1024:
        return f"{bytes_anzahl} B"
    elif bytes_anzahl < 1024 ** 2:
        return f"{bytes_anzahl / 1024:.1f} KB"
    elif bytes_anzahl < 1024 ** 3:
        return f"{bytes_anzahl / (1024 ** 2):.1f} MB"
    else:
        return f"{bytes_anzahl / (1024 ** 3):.2f} GB"

def formatiere_zeitdauer(sekunden):
    """
    Formatiert eine Zeitdauer in Sekunden in lesbares Format.
    
    Args:
        sekunden (float): Zeitdauer in Sekunden
        
    Returns:
        str: Formatierte Zeitangabe (z.B. "1h 23m 45s", "45s")
    """
    if sekunden < 60:
        return f"{sekunden:.1f}s"
    elif sekunden < 3600:
        minuten = int(sekunden // 60)
        rest_sek = int(sekunden % 60)
        return f"{minuten}m {rest_sek}s"
    else:
        stunden = int(sekunden // 3600)
        rest_min = int((sekunden % 3600) // 60)
        return f"{stunden}h {rest_min}m"

# ============================================================================
# SCHRITT 1: FTP MIRROR - FTP-Download Funktionen
# ============================================================================

def kuerze_pfad(pfad_text, max_laenge, position="middle"):
    """
    Kürzt einen langen Pfad mit Ellipsis (...).
    
    Wird nur für die Anzeige verwendet, der echte Pfad bleibt unverändert.
    
    Args:
        pfad_text (str): Zu kürzender Pfad
        max_laenge (int): Maximale Länge (0 = keine Kürzung)
        position (str): Position der Kürzung ("left", "middle", "right")
        
    Returns:
        str: Gekürzter Pfad oder Original wenn keine Kürzung nötig
    """
    if max_laenge <= 0 or len(pfad_text) <= max_laenge:
        return pfad_text
    
    if max_laenge <= 3:
        return pfad_text[:max_laenge]
    
    if position == "left":
        return "…" + pfad_text[-(max_laenge - 1):]
    elif position == "right":
        return pfad_text[:max_laenge - 1] + "…"
    else:  # middle
        links = (max_laenge - 1) // 2
        rechts = max_laenge - 1 - links
        return pfad_text[:links] + "…" + pfad_text[-rechts:]

def formatiere_anzeige_pfad(remote_pfad):
    """
    Formatiert einen Remote-Pfad für die Anzeige.
    
    Kann optional Teile des Pfades umschreiben und kürzen.
    Beeinflusst NICHT den echten FTP-Pfad!
    
    Args:
        remote_pfad (str): Originaler Remote-Pfad
        
    Returns:
        str: Formatierter Pfad für Anzeige
    """
    pfad = str(remote_pfad)
    
    # Optional: Pfad-Präfix ersetzen (für kürzere Anzeige)
    if DISPLAY_PATH_REWRITE_ENABLED and DISPLAY_PATH_REWRITE_FROM:
        if pfad.startswith(DISPLAY_PATH_REWRITE_FROM):
            pfad = DISPLAY_PATH_REWRITE_TO + pfad[len(DISPLAY_PATH_REWRITE_FROM):]
    
    # Optional: Lange Pfade kürzen
    if DISPLAY_PATH_MAXLEN > 0:
        pfad = kuerze_pfad(pfad, DISPLAY_PATH_MAXLEN, DISPLAY_PATH_ELLIPSIS)
    
    return pfad

def verbinde_ftp():
    """
    Stellt eine Verbindung zum FTP-Server her.
    
    Features:
    - Automatische Wiederholungsversuche bei Fehlern
    - Passivmodus-Unterstützung
    - Binärmodus für Downloads
    - TCP Keepalive (falls vom OS unterstützt)
    
    Returns:
        ftplib.FTP: Verbundenes FTP-Objekt
        
    Raises:
        Exception: Nach MAX_RETRIES fehlgeschlagenen Versuchen
    """
    letzter_fehler = None
    
    for versuch in range(1, MAX_RETRIES + 1):
        try:
            # Erstelle FTP-Verbindung mit Timeout
            ftp = ftplib.FTP(timeout=TIMEOUT)
            ftp.connect(FTP_HOST, FTP_PORT, timeout=TIMEOUT)
            ftp.login(FTP_USER, FTP_PASS)
            ftp.set_pasv(USE_PASSIVE)
            
            # Aktiviere TCP Keepalive (verhindert Timeout bei langen Transfers)
            try:
                ftp.sock.setsockopt(socket.SOL_SOCKET, socket.SO_KEEPALIVE, 1)
            except Exception:
                pass  # Nicht kritisch wenn es fehlschlägt
            
            # Setze Binärmodus für unveränderte Byte-Übertragung
            try:
                ftp.voidcmd("TYPE I")
            except Exception:
                pass  # Nicht kritisch wenn es fehlschlägt
            
            return ftp
            
        except Exception as e:
            letzter_fehler = e
            if versuch < MAX_RETRIES:
                print(f"[WARN] Verbindungsfehler (Versuch {versuch}/{MAX_RETRIES}): {e}")
                time.sleep(SLEEP_BETWEEN_RETRIES * versuch)
                continue
    
    # Nach allen Versuchen: Fehler weitergeben
    raise letzter_fehler

def ftp_neu_verbinden(alte_ftp):
    """
    Schließt alte FTP-Verbindung und stellt neue her.
    
    Args:
        alte_ftp: Alte FTP-Verbindung (kann None sein)
        
    Returns:
        ftplib.FTP: Neue FTP-Verbindung
    """
    # Versuche alte Verbindung zu schließen
    if alte_ftp:
        try:
            alte_ftp.close()
        except Exception:
            pass  # Ignoriere Fehler beim Schließen
    
    # Stelle neue Verbindung her
    neue_ftp = verbinde_ftp()
    setup_hash_funktionen(neue_ftp)
    return neue_ftp

def ftp_keepalive(ftp):
    """
    Sendet NOOP-Befehl um Verbindung aktiv zu halten.
    
    Args:
        ftp: FTP-Verbindung
        
    Returns:
        ftplib.FTP: FTP-Verbindung (neu verbunden bei Fehler)
    """
    try:
        ftp.voidcmd("NOOP")
        return ftp
    except Exception:
        # Bei Fehler: Neu verbinden
        return ftp_neu_verbinden(ftp)

def setup_hash_funktionen(ftp):
    """
    Ermittelt Hash-Fähigkeiten des FTP-Servers.
    
    Prüft ob Server HASH (MD5, SHA, CRC32) oder XCRC unterstützt.
    Setzt globale Variablen für spätere Nutzung.
    
    Args:
        ftp: FTP-Verbindung
    """
    global HASH_UNTERSTUETZT, HASH_ALGO, XCRC_UNTERSTUETZT
    
    HASH_UNTERSTUETZT = False
    HASH_ALGO = None
    XCRC_UNTERSTUETZT = False
    
    try:
        # Hole Feature-Liste vom Server
        feat = ftp.sendcmd("FEAT")
        zeilen = [z.strip() for z in feat.splitlines()]
        
        # Suche nach HASH-Unterstützung
        hash_zeile = next((z for z in zeilen if z.upper().startswith("HASH")), None)
        if hash_zeile:
            tokens = hash_zeile.split()
            if len(tokens) >= 2:
                # Extrahiere verfügbare Algorithmen
                algos = []
                for teil in tokens[1].split(";"):
                    algos.append(teil.replace("*", "").strip())
                
                # Versuche bevorzugten Algorithmus zu setzen
                for bevorzugt in PREFERRED_HASH_ALGOS:
                    if bevorzugt in algos:
                        try:
                            ftp.sendcmd(f"OPTS HASH {bevorzugt}")
                            HASH_UNTERSTUETZT = True
                            HASH_ALGO = bevorzugt
                            break
                        except Exception:
                            continue
        
        # Suche nach XCRC-Unterstützung
        if any("XCRC" in z.upper() for z in zeilen):
            XCRC_UNTERSTUETZT = True
        if any("SITE" in z.upper() and "CRC" in z.upper() for z in zeilen):
            XCRC_UNTERSTUETZT = True
            
    except Exception as e:
        # Fehler nicht kritisch, fahre ohne Hash-Prüfung fort
        pass

def berechne_lokalen_hash(dateipfad, algorithmus):
    """
    Berechnet Hash-Wert einer lokalen Datei.
    
    Args:
        dateipfad (str): Pfad zur Datei
        algorithmus (str): Hash-Algorithmus ("CRC32", "MD5", "SHA-1", etc.)
        
    Returns:
        str: Hexadezimaler Hash-Wert
    """
    algo_norm = algorithmus.replace("-", "").lower()
    
    if algo_norm == "crc32":
        # CRC32 via zlib (sehr schnell)
        crc = 0
        with open(dateipfad, "rb") as f:
            while True:
                chunk = f.read(1024 * 1024)
                if not chunk:
                    break
                crc = zlib.crc32(chunk, crc)
        return f"{crc & 0xFFFFFFFF:08x}"
    else:
        # Andere Algorithmen via hashlib
        h = hashlib.new(algo_norm)
        with open(dateipfad, "rb") as f:
            while True:
                chunk = f.read(1024 * 1024)
                if not chunk:
                    break
                h.update(chunk)
        return h.hexdigest()

def ftp_dateigroesse(ftp, remote_pfad, fakten=None):
    """
    Ermittelt Dateigröße vom FTP-Server.
    
    Args:
        ftp: FTP-Verbindung
        remote_pfad (str): Pfad zur Datei auf dem Server
        fakten (dict): Optional MLSD-Fakten
        
    Returns:
        int: Dateigröße in Bytes oder None bei Fehler
    """
    # Zuerst aus MLSD-Fakten versuchen (wenn vorhanden)
    if fakten:
        groesse = fakten.get("size") or fakten.get("sizd")
        if groesse and str(groesse).isdigit():
            return int(groesse)
    
    # Sonst SIZE-Befehl verwenden
    for versuch in range(1, MAX_RETRIES + 1):
        try:
            return ftp.size(remote_pfad)
        except Exception:
            if versuch < MAX_RETRIES:
                time.sleep(SLEEP_BETWEEN_RETRIES)
                continue
    
    return None  # Konnte Größe nicht ermitteln

def lade_datei_herunter(ftp, remote_pfad, lokaler_pfad, fakten=None):
    """
    Lädt eine einzelne Datei vom FTP-Server herunter.
    
    Features:
    - Resume bei abgebrochenen Downloads
    - Hash-Verifikation (wenn Server unterstützt)
    - Fortschrittsanzeige mit Geschwindigkeit
    - Automatische Wiederholungsversuche
    
    Args:
        ftp: FTP-Verbindung
        remote_pfad (str): Pfad zur Datei auf dem Server
        lokaler_pfad (str): Lokaler Zielpfad
        fakten (dict): Optional MLSD-Fakten
        
    Returns:
        dict: Statistiken über den Download
            - action: "downloaded", "skipped" oder "resumed"
            - bytes: Anzahl übertragener Bytes
            - secs: Benötigte Zeit in Sekunden
            - mbps: Durchschnittsgeschwindigkeit in MB/s
            - verify: "OK", "FAILED" oder "SKIPPED"
    """
    global gesamt_bytes_geladen, gesamt_sekunden_transfer, gesamt_dateien_sync
    
    # Ermittle Remote-Dateigröße
    remote_groesse = ftp_dateigroesse(ftp, remote_pfad, fakten)
    
    # Prüfe ob lokale Datei bereits existiert
    if os.path.exists(lokaler_pfad):
        lokale_groesse = os.path.getsize(lokaler_pfad)
    
        # Skip wenn Dateien gleich groß sind
        if SKIP_EXISTING_SAME_SIZE and remote_groesse and lokale_groesse == remote_groesse:
            gesamt_dateien_sync += 1
            return {
                'action': 'skipped',
                'bytes': 0,
                'secs': 0,
                'mbps': 0,
                'verify': 'SKIPPED'
            }
    
    # Bereite Download vor
    start_position = 0
    modus = "downloaded"
    
    # Resume-Unterstützung
    if RESUME_PARTIAL and os.path.exists(lokaler_pfad):
        start_position = os.path.getsize(lokaler_pfad)
        if start_position > 0 and (not remote_groesse or start_position < remote_groesse):
            modus = "resumed"
    
    # Download durchführen
    bytes_geladen = 0
    start_zeit = time.time()
    
    try:
        with open(lokaler_pfad, 'ab' if start_position > 0 else 'wb') as f:
            # Setze Start-Position (REST-Befehl)
            if start_position > 0:
                ftp.voidcmd(f"REST {start_position}")
            
            # Download-Callback für Fortschritt
            def callback(daten):
                nonlocal bytes_geladen
                f.write(daten)
                bytes_geladen += len(daten)
            
            # Starte Download
            ftp.retrbinary(f"RETR {remote_pfad}", callback, BLOCKSIZE)
    
    except Exception as e:
        print(f"\n[ERROR] Download fehlgeschlagen: {e}")
        raise
    
    ende_zeit = time.time()
    dauer = max(ende_zeit - start_zeit, 0.001)  # Verhindere Division durch 0
    
    # Aktualisiere globale Statistiken
    gesamt_bytes_geladen += bytes_geladen
    gesamt_sekunden_transfer += dauer
    gesamt_dateien_sync += 1
    
    # Berechne Geschwindigkeit
    mbps = (bytes_geladen / (1024 * 1024)) / dauer if dauer > 0 else 0
    
    # Hash-Verifikation (wenn aktiviert und verfügbar)
    verify_status = "SKIPPED"
    
    # Hash-Verifikation (wenn aktiviert und verfügbar)
    verify_status = "SKIPPED"
    
    # Prüfe ob Hash-Verifikation aktiviert ist
    if VERIFY_HASH:
        # Wenn Server Hash unterstützt, verwende Hash
        if HASH_UNTERSTUETZT and HASH_ALGO:
            try:
                # Berechne lokalen Hash
                lokaler_hash = berechne_lokalen_hash(lokaler_pfad, HASH_ALGO)

                # Hole Remote-Hash
                try:
                    antwort = ftp.sendcmd(f"HASH {remote_pfad}")
                    remote_hash = antwort.split()[-1].lower()

                    # Vergleiche Hashes
                    if lokaler_hash.lower() == remote_hash:
                        verify_status = "OK"
                    else:
                        verify_status = "FAILED"
                except Exception:
                    # Hash-Befehl fehlgeschlagen - Fallback auf Größenvergleich
                    if remote_groesse and os.path.exists(lokaler_pfad):
                        aktuelle_lokale_groesse = os.path.getsize(lokaler_pfad)
                        if aktuelle_lokale_groesse == remote_groesse:
                            verify_status = "SIZE_ONLY"
                        else:
                            verify_status = "SIZE_MISMATCH"
                    else:
                        verify_status = "UNAVAILABLE"
            except Exception:
                verify_status = "ERROR"
        else:
            # Server unterstützt keinen Hash - Fallback auf Größenvergleich
            if os.path.exists(lokaler_pfad):
                if remote_groesse:
                    # Remote-Größe bekannt - vergleiche
                    aktuelle_lokale_groesse = os.path.getsize(lokaler_pfad)
                    if aktuelle_lokale_groesse == remote_groesse:
                        verify_status = "SIZE_ONLY"
                    else:
                        verify_status = "SIZE_MISMATCH"
                else:
                    # Remote-Größe unbekannt - hole sie jetzt
                    try:
                        remote_groesse_neu = ftp.size(remote_pfad)
                        if remote_groesse_neu:
                            aktuelle_lokale_groesse = os.path.getsize(lokaler_pfad)
                            if aktuelle_lokale_groesse == remote_groesse_neu:
                                verify_status = "SIZE_ONLY"
                            else:
                                verify_status = "SIZE_MISMATCH"
                        else:
                            verify_status = "NO_REMOTE_SIZE"
                    except Exception:
                        verify_status = "NO_REMOTE_SIZE"
    
    # Setze Zeitstempel (wenn aktiviert)
    if SET_MTIME and fakten and 'modify' in fakten:
        try:
            # Parse MLSD modify timestamp (YYYYMMDDHHMMSS, UTC)
            modify_str = fakten['modify']
            dt = datetime.strptime(modify_str, "%Y%m%d%H%M%S").replace(tzinfo=timezone.utc)
            timestamp = dt.timestamp()
            os.utime(lokaler_pfad, (timestamp, timestamp))
        except Exception:
            pass  # Nicht kritisch wenn Zeitstempel nicht gesetzt werden kann
    
    return {
        'action': modus,
        'bytes': bytes_geladen,
        'secs': dauer,
        'mbps': mbps,
        'verify': verify_status
    }

def liste_ftp_verzeichnis(ftp, remote_ordner):
    """
    Listet Inhalt eines FTP-Verzeichnisses auf.
    
    Bevorzugt MLSD (maschinenlesbar), fällt zurück auf NLST wenn nötig.
    
    Args:
        ftp: FTP-Verbindung
        remote_ordner (str): Pfad zum Verzeichnis
        
    Returns:
        list: Liste von Tuples (name, typ, fakten)
            - name: Datei-/Ordnername
            - typ: "file" oder "dir"
            - fakten: Dictionary mit MLSD-Informationen
    """
    ftp = ftp_keepalive(ftp)
    
    for versuch in range(1, MAX_RETRIES + 1):
        aktueller_ordner = None
        
        try:
            # Merke aktuellen Ordner
            try:
                aktueller_ordner = ftp.pwd()
            except Exception:
                ftp = ftp_neu_verbinden(ftp)
                aktueller_ordner = None
            
            # Wechsle in Zielordner
            ftp.cwd(remote_ordner)
            
            eintraege = []
            
            # Versuche MLSD (moderne, strukturierte Auflistung)
            try:
                for name, fakten in ftp.mlsd():
                    if name in (".", ".."):
                        continue
                    
                    typ = (fakten.get("type") or "").lower()
                    
                    if typ in ("dir", "cdir", "pdir"):
                        eintraege.append((name, "dir", fakten))
                    elif typ == "file":
                        eintraege.append((name, "file", fakten))
                    else:
                        # Typ unklar, teste mit CWD
                        ist_ordner = False
                        aktuell = ftp.pwd()
                        try:
                            ftp.cwd(name)
                            ftp.cwd(aktuell)
                            ist_ordner = True
                        except Exception:
                            pass
                        
                        if ist_ordner:
                            eintraege.append((name, "dir", fakten))
                        else:
                            eintraege.append((name, "file", fakten))
                
                return eintraege
                
            except Exception:
                # MLSD fehlgeschlagen, versuche NLST (einfache Namensliste)
                try:
                    namen = ftp.nlst()
                except Exception:
                    namen = []
                
                eintraege = []
                for name in namen:
                    if name in (".", ".."):
                        continue
                    
                    # Teste ob Ordner oder Datei
                    ist_ordner = False
                    aktuell = ftp.pwd()
                    try:
                        ftp.cwd(name)
                        ftp.cwd(aktuell)
                        ist_ordner = True
                    except Exception:
                        pass
                    
                    if ist_ordner:
                        eintraege.append((name, "dir", {}))
                    else:
                        eintraege.append((name, "file", {}))
                
                return eintraege
                
        except Exception as e:
            if versuch < MAX_RETRIES:
                ftp = ftp_neu_verbinden(ftp)
                time.sleep(SLEEP_BETWEEN_RETRIES)
                continue
            else:
                print(f"[WARN] Listing fehlgeschlagen für '{formatiere_anzeige_pfad(remote_ordner)}': {e}")
                return []
        
        finally:
            # Stelle Original-Ordner wieder her
            if aktueller_ordner:
                try:
                    ftp.cwd(aktueller_ordner)
                except Exception:
                    pass
    
    return []

def spiegle_ftp_ordner(ftp, basis_remote, basis_lokal):
    """
    Spiegelt einen FTP-Ordner rekursiv auf lokale Festplatte.
    
    Durchläuft alle Unterordner und lädt alle Dateien herunter.
    Behält die Ordnerstruktur bei.
    
    Args:
        ftp: FTP-Verbindung
        basis_remote (str): Startpfad auf dem Server
        basis_lokal (str): Lokaler Zielordner
    """
    global gesamt_ordner_sync
    
    def rekursiv_spiegeln(remote_ordner):
        """Innere Funktion für rekursive Verarbeitung."""
        global gesamt_ordner_sync
        
        # Berechne lokalen Pfad
        relativer_pfad = os.path.relpath(remote_ordner, basis_remote)
        if relativer_pfad == ".":
            lokaler_ordner = basis_lokal
        else:
            lokaler_ordner = os.path.join(basis_lokal, relativer_pfad)
        
        # Erstelle lokalen Ordner
        os.makedirs(lokaler_ordner, exist_ok=True)
        gesamt_ordner_sync += 1
        
        print(f"\n[DIR] {formatiere_anzeige_pfad(remote_ordner)}")
        
        # Hole Verzeichnis-Listing
        eintraege = liste_ftp_verzeichnis(ftp, remote_ordner)
        
        # Trenne in Ordner und Dateien
        ordner = [(n, f) for (n, t, f) in eintraege if t == "dir"]
        dateien = [(n, f) for (n, t, f) in eintraege if t == "file"]
        
        # Rekursiv in Unterordner
        for name, fakten in ordner:
            naechster_remote = normpath(pjoin(remote_ordner, name))
            rekursiv_spiegeln(naechster_remote)
        
        # Lade Dateien herunter
        for name, fakten in dateien:
            remote_pfad = normpath(pjoin(remote_ordner, name))
            lokaler_pfad = os.path.join(lokaler_ordner, name)
            
            # Zeige Dateigröße an
            groesse_text = ""
            datei_groesse = ftp_dateigroesse(ftp, remote_pfad, fakten)
            if datei_groesse:
                groesse_text = f" ({formatiere_dategroesse(datei_groesse)})"
            
            print(f"[FILE] {formatiere_anzeige_pfad(remote_pfad)}{groesse_text} ... ", end="", flush=True)
            
            try:
                stats = lade_datei_herunter(ftp, remote_pfad, lokaler_pfad, fakten)
                print(f"{stats['action'].upper()}: {formatiere_dategroesse(stats['bytes'])} "
                      f"in {formatiere_zeitdauer(stats['secs'])} @ {stats['mbps']:.1f} MB/s "
                      f"[Verify: {stats['verify']}]")
            except Exception as e:
                print(f"FEHLER: {e}")
    
    # Starte rekursive Spiegelung
    basis_remote_norm = normpath("/" + basis_remote.lstrip("/"))
    rekursiv_spiegeln(basis_remote_norm)

def berechne_ordner_groesse_gb(ordner):
    """
    Berechnet die Gesamtgröße eines Ordners in GB.
    
    Args:
        ordner (str): Pfad zum Ordner
        
    Returns:
        float: Größe in Gigabyte (GiB)
    """
    gesamt_groesse = 0
    for wurzel, ordner_liste, datei_liste in os.walk(ordner):
        for datei in datei_liste:
            try:
                pfad = os.path.join(wurzel, datei)
                gesamt_groesse += os.path.getsize(pfad)
            except Exception:
                pass  # Ignoriere nicht lesbare Dateien
    
    return gesamt_groesse / (1024 ** 3)

def fuehre_ftp_mirror_aus():
    """
    Hauptfunktion für Schritt 1: FTP Mirror.
    
    Lädt WHDLoad-Archive vom konfigurierten FTP-Server herunter.
    Zeigt detaillierte Statistiken über Downloads, Geschwindigkeit und Speichernutzung.
    """
    zeige_schritt_header(
        1,
        "FTP MIRROR",
        f"Lädt WHDLoad-Archive von {FTP_HOST} herunter"
    )
    
    print(f"Remote-Pfad: {formatiere_anzeige_pfad(REMOTE_PATH)}")
    print(f"Lokaler Zielordner: {MIRROR_ORDNER}")
    print(f"Passivmodus: {'Ja' if USE_PASSIVE else 'Nein'}")
    print(f"Hash-Verifikation: {VERIFY_HASH}")
    print()
    
    # Starte Zeitmessung
    start_zeit = datetime.now()
    print(f"Startzeit: {start_zeit.strftime('%Y-%m-%d %H:%M:%S')}\n")
    
    # Verbinde zu FTP-Server
    print("Verbinde zu FTP-Server...")
    ftp = verbinde_ftp()
    print("Verbindung hergestellt!")
    
    # Ermittle Server-Fähigkeiten
    setup_hash_funktionen(ftp)
    if HASH_UNTERSTUETZT:
        print(f"Hash-Verifikation verfügbar: {HASH_ALGO}")
    if XCRC_UNTERSTUETZT:
        print(f"XCRC verfügbar")
    print()
    
    try:
        # Spiegle Verzeichnis
        spiegle_ftp_ordner(ftp, REMOTE_PATH, MIRROR_ORDNER)
        
    finally:
        # Schließe FTP-Verbindung
        try:
            ftp.quit()
        except Exception:
            try:
                ftp.close()
            except Exception:
                pass
    
    # Ende Zeitmessung
    ende_zeit = datetime.now()
    dauer = ende_zeit - start_zeit
    
    # Zeige Statistiken
    print("\n" + "=" * SEPARATOR_LAENGE)
    print("MIRROR ABGESCHLOSSEN - STATISTIK")
    print("=" * SEPARATOR_LAENGE)
    print(f"Startzeit: {start_zeit.strftime('%Y-%m-%d %H:%M:%S')}")
    print(f"Endzeit: {ende_zeit.strftime('%Y-%m-%d %H:%M:%S')}")
    print(f"Gesamtdauer: {formatiere_zeitdauer(dauer.total_seconds())}")
    print(f"\nOrdner verarbeitet: {gesamt_ordner_sync}")
    print(f"Dateien verarbeitet: {gesamt_dateien_sync}")
    print(f"Daten heruntergeladen: {formatiere_dategroesse(gesamt_bytes_geladen)}")
    
    if gesamt_sekunden_transfer > 0:
        durchschnitt_mbps = (gesamt_bytes_geladen / (1024 * 1024)) / gesamt_sekunden_transfer
        print(f"Ø Download-Geschwindigkeit: {durchschnitt_mbps:.2f} MB/s")
    
    # Berechne Ordnergröße
    ordner_groesse = berechne_ordner_groesse_gb(MIRROR_ORDNER)
    print(f"\nSpeicherbelegung in '{MIRROR_ORDNER}': {ordner_groesse:.2f} GB")
    print("=" * SEPARATOR_LAENGE)

# ============================================================================
# SCHRITT 2: SORT - Sortierung nach Amiga-System
# ============================================================================

def parse_lha_dateiname(dateiname):
    """
    Analysiert einen WHDLoad-Dateinamen und extrahiert Komponenten.
    
    Format: SpielName_vVersion_Sprachen_ZusatzInfo_Zahlencode.lha
    
    Args:
        dateiname (str): Zu analysierender Dateiname
        
    Returns:
        dict: Dictionary mit extrahierten Informationen oder None
            - filename: Original-Dateiname
            - game_name: Name des Spiels
            - version: Versionsnummer (oder None)
            - languages: Liste von Sprachcodes
            - additional_info: Liste mit Zusatzinformationen (AGA, CD32, etc.)
    """
    if not dateiname.endswith(ARCHIV_ENDUNG):
        return None
    
    # Entferne .lha Endung
    name_ohne_endung = dateiname[:-len(ARCHIV_ENDUNG)]
    
    # Entferne 4-stellige Zahlencodes am Ende
    name_bereinigt = re.sub(ZAHLENCODE_PATTERN, '', name_ohne_endung)
    
    # Teile an Unterstrichen
    teile = name_bereinigt.split('_')
    
    if len(teile) < 1:
        return None
    
    # Erster Teil ist Spielname
    spiel_name = teile[0]
    
    # Initialisiere Komponenten
    version = None
    sprachen = []
    zusatzinfo = []
    
    # Verarbeite restliche Teile
    for i, teil in enumerate(teile[1:], 1):
        # Versionsnummer?
        if teil.startswith('v') or teil.startswith('V'):
            version = teil
        # Sprachcode?
        elif re.match(SPRACHCODE_PATTERN, teil):
            # Extrahiere einzelne 2-Buchstaben-Codes
            codes = re.findall(r'[A-Z][a-z]', teil)
            sprachen.extend(codes)
        else:
            # Alles andere ist Zusatzinfo
            zusatzinfo.append(teil)
    
    # Standard-Sprache wenn keine erkannt
    if not sprachen:
        sprachen = [STANDARD_SPRACHE]
    
    return {
        'filename': dateiname,
        'game_name': spiel_name,
        'version': version,
        'languages': sprachen,
        'additional_info': zusatzinfo
    }

def sortiere_nach_system(quellordner, zielordner):
    """
    Sortiert WHDLoad-Archive nach Amiga-Systemtyp.
    
    Sortierlogik:
    - AGA → Amiga 1200
    - CD32 → Amiga CD32
    - CDTV → Amiga CDTV
    - Alles andere → Amiga 500
    
    Args:
        quellordner (str): Ordner mit unsorttierten Archiven
        zielordner (str): Zielordner für sortierte Archive
    """
    print(f"Analysiere Ordner: {quellordner}")
    print("=" * SEPARATOR_LAENGE)
    
    quell_pfad = Path(quellordner)
    ziel_pfad = Path(zielordner)
    
    if not quell_pfad.exists():
        print(f"FEHLER: Quellordner '{quellordner}' existiert nicht!")
        return
    
    # Erstelle Zielordner
    zielordner_definitionen = {
        'amiga1200': ziel_pfad / ORDNER_AMIGA1200,
        'amigacd32': ziel_pfad / ORDNER_AMIGACD32,
        'amigacdtv': ziel_pfad / ORDNER_AMIGACDTV,
        'amiga500': ziel_pfad / ORDNER_AMIGA500
    }
    
    print(f"\nErstelle Zielordner...")
    for name, pfad in zielordner_definitionen.items():
        pfad.mkdir(parents=True, exist_ok=True)
        print(f"  ✓ {name}")
    
    # Finde alle .lha Dateien
    print(f"\nSuche nach {ARCHIV_ENDUNG}-Dateien...")
    lha_dateien = list(quell_pfad.rglob(f'*{ARCHIV_ENDUNG}'))
    print(f"Gefunden: {len(lha_dateien)} Dateien\n")
    
    # Statistiken
    statistik = {
        'amiga1200': 0,
        'amigacd32': 0,
        'amigacdtv': 0,
        'amiga500': 0
    }
    
    # Sprach- und Info-Statistiken
    zusatzinfo_zaehler = defaultdict(int)
    sprach_zaehler = defaultdict(int)
    
    # Verarbeite jede Datei
    print("Sortiere Dateien...")
    for datei_pfad in lha_dateien:
        # Parse Dateiname
        info = parse_lha_dateiname(datei_pfad.name)
        if not info:
            continue
        
        # Sammle Statistiken
        for zusatz in info['additional_info']:
            zusatzinfo_zaehler[zusatz] += 1
        for sprache in info['languages']:
            sprach_zaehler[sprache] += 1
        
        # Bestimme Zielsystem
        zusatz_gross = [z.upper() for z in info['additional_info']]
        
        if 'AGA' in zusatz_gross:
            ziel = zielordner_definitionen['amiga1200']
            kategorie = 'amiga1200'
        elif 'CD32' in zusatz_gross:
            ziel = zielordner_definitionen['amigacd32']
            kategorie = 'amigacd32'
        elif 'CDTV' in zusatz_gross:
            ziel = zielordner_definitionen['amigacdtv']
            kategorie = 'amigacdtv'
        else:
            ziel = zielordner_definitionen['amiga500']
            kategorie = 'amiga500'
        
        # Für Amiga 500: Behalte Unterordner-Struktur
        if kategorie == 'amiga500' and datei_pfad.parent != quell_pfad:
            relativer_pfad = datei_pfad.parent.relative_to(quell_pfad)
            ziel_pfad_datei = ziel / relativer_pfad
            ziel_pfad_datei.mkdir(parents=True, exist_ok=True)
        else:
            ziel_pfad_datei = ziel
        
        # Kopiere Datei
        ziel_datei = ziel_pfad_datei / datei_pfad.name
        try:
            shutil.copy2(datei_pfad, ziel_datei)
            statistik[kategorie] += 1
        except Exception as e:
            print(f"FEHLER beim Kopieren von {datei_pfad.name}: {e}")
    
    # Zeige Statistiken
    print("\n" + "=" * SEPARATOR_LAENGE)
    print("SORTIERUNG ABGESCHLOSSEN - STATISTIK")
    print("=" * SEPARATOR_LAENGE)
    
    print(f"\nDateien pro System:")
    print(f"  Amiga 1200 (AGA): {statistik['amiga1200']:4d}")
    print(f"  Amiga CD32:       {statistik['amigacd32']:4d}")
    print(f"  Amiga CDTV:       {statistik['amigacdtv']:4d}")
    print(f"  Amiga 500 (OCS):  {statistik['amiga500']:4d}")
    print(f"  {'-' * 30}")
    print(f"  Gesamt:           {sum(statistik.values()):4d}")
    
    print(f"\nTop 10 Zusatzinformationen:")
    for info, anzahl in sorted(zusatzinfo_zaehler.items(), key=lambda x: x[1], reverse=True)[:10]:
        print(f"  {info:20s}: {anzahl:4d}")
    
    print(f"\nSprachen gefunden:")
    for sprache, anzahl in sorted(sprach_zaehler.items(), key=lambda x: x[1], reverse=True):
        print(f"  {sprache:10s}: {anzahl:4d}")
    
    print("=" * SEPARATOR_LAENGE)

def fuehre_sort_aus():
    """
    Hauptfunktion für Schritt 2: Sort.
    
    Sortiert heruntergeladene Archive nach Amiga-Systemtyp.
    """
    zeige_schritt_header(
        2,
        "SORT",
        "Sortiert Archive nach Amiga-Systemtyp (500/1200/CD32/CDTV)"
    )
    
    sortiere_nach_system(SORT_QUELLORDNER, SORT_ZIELORDNER)

# ============================================================================
# SCHRITT 3: COMPARE - Verzeichnis-Vergleich
# ============================================================================

def sammle_dateinamen(verzeichnis):
    """
    Sammelt alle Dateinamen rekursiv aus einem Verzeichnis.
    
    Args:
        verzeichnis (str): Zu durchsuchendes Verzeichnis
        
    Returns:
        set: Menge aller Dateinamen (ohne Pfade)
    """
    dateinamen = set()
    try:
        for wurzel, ordner, dateien in os.walk(verzeichnis):
            for datei in dateien:
                dateinamen.add(datei)
        return dateinamen
    except Exception as e:
        print(f"Fehler beim Lesen von {verzeichnis}: {e}")
        sys.exit(1)

def sammle_dateinamen_mit_pfaden(verzeichnis):
    """
    Sammelt alle Dateinamen mit relativen Pfaden aus einem Verzeichnis.
    
    Args:
        verzeichnis (str): Zu durchsuchendes Verzeichnis
        
    Returns:
        dict: Dictionary mit Dateinamen als Keys und Listen von Pfaden als Values
    """
    dateien_dict = {}
    try:
        for wurzel, ordner, dateien in os.walk(verzeichnis):
            for datei in dateien:
                voller_pfad = os.path.join(wurzel, datei)
                relativer_pfad = os.path.relpath(voller_pfad, verzeichnis)
                
                if datei not in dateien_dict:
                    dateien_dict[datei] = []
                dateien_dict[datei].append(relativer_pfad)
        
        return dateien_dict
    except Exception as e:
        print(f"Fehler beim Lesen von {verzeichnis}: {e}")
        sys.exit(1)

def vergleiche_verzeichnisse(ordner1, ordner2):
    """
    Vergleicht zwei Verzeichnisse und zeigt Unterschiede.
    
    Args:
        ordner1 (str): Erstes Verzeichnis
        ordner2 (str): Zweites Verzeichnis
    """
    # Prüfe ob Verzeichnisse existieren
    if not os.path.isdir(ordner1):
        print(f"FEHLER: '{ordner1}' ist kein gültiges Verzeichnis!")
        return
    if not os.path.isdir(ordner2):
        print(f"FEHLER: '{ordner2}' ist kein gültiges Verzeichnis!")
        return
    
    print(f"Vergleiche Verzeichnisse:")
    print(f"  Ordner 1: {ordner1}")
    print(f"  Ordner 2: {ordner2}")
    print("-" * COMPARE_AUSGABE_BREITE)
    
    # Sammle Dateinamen
    dateien1 = sammle_dateinamen(ordner1)
    dateien2 = sammle_dateinamen(ordner2)
    
    # Vergleiche
    nur_in_1 = dateien1 - dateien2
    nur_in_2 = dateien2 - dateien1
    gemeinsam = dateien1 & dateien2
    
    # Statistik
    print(f"\nStatistik:")
    print(f"  Dateien in Ordner 1: {len(dateien1)}")
    print(f"  Dateien in Ordner 2: {len(dateien2)}")
    print(f"  Gemeinsame Dateien:  {len(gemeinsam)}")
    print(f"  Nur in Ordner 1:     {len(nur_in_1)}")
    print(f"  Nur in Ordner 2:     {len(nur_in_2)}")
    print("-" * COMPARE_AUSGABE_BREITE)
    
    # Zeige Unterschiede
    if nur_in_1:
        print(f"\nDateien nur in {ordner1}:")
        for datei in sorted(nur_in_1):
            print(f"  - {datei}")
    
    if nur_in_2:
        print(f"\nDateien nur in {ordner2}:")
        for datei in sorted(nur_in_2):
            print(f"  - {datei}")
    
    if not nur_in_1 and not nur_in_2:
        print("\n✓ Beide Verzeichnisse enthalten die gleichen Dateinamen!")
    
    # Detaillierte Ausgabe mit Pfaden
    print("\n" + "=" * COMPARE_AUSGABE_BREITE)
    print("Detaillierte Ausgabe mit Pfaden:")
    print("=" * COMPARE_AUSGABE_BREITE)
    
    dateien1_pfade = sammle_dateinamen_mit_pfaden(ordner1)
    dateien2_pfade = sammle_dateinamen_mit_pfaden(ordner2)
    
    if nur_in_1:
        print(f"\nDateien nur in {ordner1} (mit Pfaden):")
        for datei in sorted(nur_in_1):
            for pfad in dateien1_pfade[datei]:
                print(f"  {pfad}")
    
    if nur_in_2:
        print(f"\nDateien nur in {ordner2} (mit Pfaden):")
        for datei in sorted(nur_in_2):
            for pfad in dateien2_pfade[datei]:
                print(f"  {pfad}")

def fuehre_compare_aus():
    """
    Hauptfunktion für Schritt 3: Compare.
    Vergleicht zwei Verzeichnisse und zeigt Unterschiede.
    """
    zeige_schritt_header(
        3,
        "COMPARE",
        "Vergleicht zwei Verzeichnisse und zeigt Unterschiede"
    )
    
    # Zeige Standard-Werte an
    print(f"Standard-Quellordner: {COMPARE_ORDNER1_DEFAULT}")
    print(f"Standard-Zielordner: {COMPARE_ORDNER2_DEFAULT}")
    print()
    
    # Frage Benutzer mit vorausgefüllten Werten
    ordner1_eingabe = input(f"Quellordner [{COMPARE_ORDNER1_DEFAULT}]: ").strip()
    ordner1 = ordner1_eingabe if ordner1_eingabe else COMPARE_ORDNER1_DEFAULT
    
    ordner2_eingabe = input(f"Zielordner [{COMPARE_ORDNER2_DEFAULT}]: ").strip()
    ordner2 = ordner2_eingabe if ordner2_eingabe else COMPARE_ORDNER2_DEFAULT
    
    print()
    vergleiche_verzeichnisse(ordner1, ordner2)

# ============================================================================
# SCHRITT 4: FINALIZE - Sprachauswahl und Finalisierung
# ============================================================================

def parse_finalize_dateiname(dateiname):
    """
    Parst WHDLoad-Dateiname für Finalize-Schritt.
    
    Ähnlich wie parse_lha_dateiname, aber mit zusätzlichen
    Schlüsseln für Versionierung.
    
    Args:
        dateiname (str): Zu parsender Dateiname
        
    Returns:
        dict: Dictionary mit Informationen oder None
    """
    if not dateiname.endswith(ARCHIV_ENDUNG):
        return None
    
    # Entferne Endung
    name_ohne_endung = dateiname[:-len(ARCHIV_ENDUNG)]
    
    # Entferne Zahlencodes
    name_bereinigt = re.sub(ZAHLENCODE_PATTERN, '', name_ohne_endung)
    
    # Teile an Unterstrichen
    teile = name_bereinigt.split('_')
    
    if len(teile) < 1:
        return None
    
    # Initialisiere Ergebnis
    ergebnis = {
        'original': dateiname,
        'game_name': teile[0],
        'version': None,
        'language_codes': [],
        'additional_info': [],
        'raw_parts': teile
    }
    
    # Suche Version
    versions_idx = -1
    for i, teil in enumerate(teile[1:], 1):
        if teil.startswith('v') or teil.startswith('V'):
            ergebnis['version'] = teil
            versions_idx = i
            break
    
    # Verarbeite Teile nach Version
    if versions_idx > 0:
        for teil in teile[versions_idx + 1:]:
            # Sprachcode?
            if re.match(SPRACHCODE_PATTERN, teil):
                codes = re.findall(r'[A-Z][a-z]', teil)
                ergebnis['language_codes'].extend(codes)
            else:
                ergebnis['additional_info'].append(teil)
    
    # Standard-Sprache
    if not ergebnis['language_codes']:
        ergebnis['language_codes'] = [STANDARD_SPRACHE]
    
    # Erstelle Schlüssel
    ergebnis['version_key'] = (
        f"{ergebnis['game_name']}_"
        f"{ergebnis['version'] or 'noversion'}_"
        f"{''.join(sorted(ergebnis['language_codes']))}"
    )
    
    ergebnis['full_key'] = (
        f"{ergebnis['version_key']}_"
        f"{'_'.join(ergebnis['additional_info'])}"
    )
    
    return ergebnis

def analysiere_finalize_ordner(quellordner, unterordner):
    """
    Durchsucht Unterordner nach .lha Dateien.
    
    Args:
        quellordner (Path): Basis-Quellordner
        unterordner (str): Name des Unterordners
        
    Returns:
        list: Liste von geparsten Datei-Informationen
    """
    ordner_pfad = quellordner / unterordner
    
    if not ordner_pfad.exists():
        return None
    
    dateien = []
    for wurzel, ordner, dateinamen in os.walk(ordner_pfad):
        for dateiname in dateinamen:
            if dateiname.endswith(ARCHIV_ENDUNG):
                geparst = parse_finalize_dateiname(dateiname)
                if geparst:
                    geparst['full_path'] = os.path.join(wurzel, dateiname)
                    geparst['relative_path'] = os.path.relpath(
                        os.path.join(wurzel, dateiname),
                        ordner_pfad
                    )
                    geparst['subdir'] = unterordner
                    dateien.append(geparst)
    
    return dateien

def zeige_versionsauswahl_details(spielname, ausgewaehlte, entfernte):
    """
    Zeigt detaillierte Informationen über ausgewählte und entfernte Versionen.
    
    Format entspricht der Ausgabe in whdload_console_output.txt.
    
    Args:
        spielname (str): Name des Spiels
        ausgewaehlte (list): Liste ausgewählter Dateien
        entfernte (list): Liste entfernter Dateien
    """
    print(f"\nSpiel: {spielname}")
    print(f"  Ausgewählt: {len(ausgewaehlte)} Version(en)")
    for datei in ausgewaehlte:
        version_str = datei['version'] or 'unbekannt'
        sprachen_str = ', '.join(datei['language_codes'])
        zusatz_liste = datei['additional_info']
        zusatz_display = f" [{', '.join(zusatz_liste)}]" if zusatz_liste else ''
        
        print(f"    ✓ {datei['original']}")
        print(f"      (Version: {version_str}, Sprachen: {sprachen_str}{zusatz_display})")
    
    if entfernte:
        print(f"  Entfernt: {len(entfernte)} Version(en)")
        for datei in entfernte:
            version_str = datei['version'] or 'unbekannt'
            sprachen_str = ', '.join(datei['language_codes'])
            zusatz_liste = datei['additional_info']
            zusatz_display = f" [{', '.join(zusatz_liste)}]" if zusatz_liste else ''
            
            print(f"    ✗ {datei['original']}")
            print(f"      (Version: {version_str}, Sprachen: {sprachen_str}{zusatz_display})")

def waehle_beste_versionen(spiel_dateien):
    """
    Wählt beste Sprachversionen eines Spiels.
    
    Bevorzugt Sprachen nach SPRACH_PRIORITAET.
    Behält ALLE Versionen mit der gewählten Sprache.
    
    Args:
        spiel_dateien (list): Liste aller Dateien eines Spiels
        
    Returns:
        tuple: (ausgewaehlte, entfernte)
    """
    # Gruppiere nach Sprache
    nach_sprache = defaultdict(list)
    for datei in spiel_dateien:
        sprach_key = ''.join(sorted(datei['language_codes']))
        nach_sprache[sprach_key].append(datei)
    
    ausgewaehlte = []
    entfernte = []
    
    # Durchlaufe Prioritäten
    for prio_sprache in SPRACH_PRIORITAET:
        passende = [
            f for f in spiel_dateien
            if prio_sprache in f['language_codes']
        ]
        
        if passende:
            ausgewaehlte = passende
            entfernte = [f for f in spiel_dateien if f not in passende]
            break
    
    # Fallback
    if not ausgewaehlte:
        erster_sprach_key = list(nach_sprache.keys())[0]
        ausgewaehlte = nach_sprache[erster_sprach_key]
        entfernte = [f for f in spiel_dateien if f not in ausgewaehlte]
    
    return ausgewaehlte, entfernte

def verarbeite_finalize_unterordner(quellordner, zielordner, unterordner, dateien_kopieren=True):
    """
    Verarbeitet einen Plattform-Unterordner für Finalize.
    
    Args:
        quellordner (Path): Quellverzeichnis
        zielordner (Path): Zielverzeichnis
        unterordner (str): Name des Unterordners
        dateien_kopieren (bool): Dateien kopieren oder nur analysieren
        
    Returns:
        dict: Ergebnis-Dictionary mit Statistiken
    """
    print(f"\n{'=' * SEPARATOR_LAENGE}")
    print(f"Verarbeite: {unterordner}")
    print(f"{'=' * SEPARATOR_LAENGE}\n")
    
    # Analysiere Ordner
    dateien = analysiere_finalize_ordner(quellordner, unterordner)
    if not dateien:
        print(f"Keine {ARCHIV_ENDUNG}-Dateien in {unterordner} gefunden.")
        return None
    
    print(f"Gefundene Dateien: {len(dateien)}\n")
    
    # Gruppiere nach Spielname
    spiele = defaultdict(list)
    for datei in dateien:
        spiele[datei['game_name']].append(datei)
    
    print(f"Einzigartige Spiele: {len(spiele)}\n")
    
    # Statistiken
    sprach_stats = defaultdict(int)
    spiele_nach_sprache = defaultdict(set)
    
    for datei in dateien:
        for sprache in datei['language_codes']:
            sprach_stats[sprache] += 1
            spiele_nach_sprache[sprache].add(datei['game_name'])
    
    # Zeige Sprachstatistik
    print("Sprachstatistik:")
    for sprache, anzahl in sorted(sprach_stats.items(), key=lambda x: x[1], reverse=True):
        spiele_anzahl = len(spiele_nach_sprache[sprache])
        print(f"  {sprache}: {anzahl} Dateien, {spiele_anzahl} Spiele")
    print()
    
    # Wähle beste Versionen
    alle_ausgewaehlte = []
    alle_entfernte = []
    spiele_mit_mehreren_versionen = []
    
    for spiel_name, spiel_dateien in spiele.items():
        ausgewaehlte, entfernte = waehle_beste_versionen(spiel_dateien)
        alle_ausgewaehlte.extend(ausgewaehlte)
        alle_entfernte.extend(entfernte)
        # Sammle Spiele mit mehreren Versionen für detaillierte Ausgabe
        if len(spiel_dateien) > 1:
            spiele_mit_mehreren_versionen.append((spiel_name, ausgewaehlte, entfernte))

    # Zeige detaillierte Versionsauswahl
    print("\n--- VERSIONSAUSWAHL ---")
    print(f"\nSpiele mit mehreren Versionen: {len(spiele_mit_mehreren_versionen)}")
    
    # Zeige detaillierte Versionsauswahl für JEDES Spiel mit mehreren Versionen
    if spiele_mit_mehreren_versionen:
        for spielname, ausgewaehlte, entfernte in spiele_mit_mehreren_versionen:
            zeige_versionsauswahl_details(spielname, ausgewaehlte, entfernte)
    
    # Zeige Auswahl-Statistik
    print(f"Auswahl-Statistik:")
    print(f"  Ausgewählt: {len(alle_ausgewaehlte)} Dateien")
    print(f"  Entfernt:   {len(alle_entfernte)} Dateien")
    
    if len(dateien) > 0:
        prozent = (len(alle_entfernte) / len(dateien)) * 100
        print(f"  Einsparung: {prozent:.1f}%")
    
    # Kopiere Dateien (wenn aktiviert)
    if dateien_kopieren:
        # Erstelle Zielunterordner
        ziel_unterordner = zielordner / unterordner
        ziel_unterordner.mkdir(parents=True, exist_ok=True)
        
        kopiert = 0
        for datei in alle_ausgewaehlte:
            quell_datei = Path(datei['full_path'])

            # Optionales Umbenennen (Unterstriche → Leerzeichen)
            ziel_name = datei['original']
            if UNTERSTRICHE_DURCH_LEERZEICHEN:
                ziel_name = ziel_name.replace('_', ' ')

            # Für amiga500: Behalte alphabetische Unterordner-Struktur bei
            if unterordner == 'amiga500':
                # Extrahiere den relativen Pfad mit Unterordnern
                rel_path = Path(datei['relative_path'])
                if len(rel_path.parts) > 1:
                    # Es gibt Unterordner (z.B. "A/Arkanoid.lha") - behalte diese Struktur
                    ziel_mit_unterordner = ziel_unterordner / rel_path.parent
                    ziel_mit_unterordner.mkdir(parents=True, exist_ok=True)
                    ziel_datei = ziel_mit_unterordner / ziel_name
                else:
                    # Keine Unterordner vorhanden - direkt kopieren
                    ziel_datei = ziel_unterordner / ziel_name
            else:
                # Für andere Systeme (1200/CD32/CDTV): Flache Struktur ohne Unterordner
                ziel_datei = ziel_unterordner / ziel_name

            try:
                shutil.copy2(quell_datei, ziel_datei)
                kopiert += 1
            except Exception as e:
                print(f"  FEHLER: {datei['original']}: {e}")

        print(f"Erfolgreich kopiert: {kopiert} Dateien")
    
    return {
        'subdir': unterordner,
        'total': len(dateien),
        'selected': alle_ausgewaehlte,
        'removed': alle_entfernte,
        'games': spiele,
        'lang_stats': dict(sprach_stats),
        'games_by_lang': {k: list(v) for k, v in spiele_nach_sprache.items()}
    }

def fuehre_finalize_aus(dateien_kopieren=True):
    """
    Hauptfunktion für Schritt 4: Finalize.
    
    Wählt beste Sprachversionen aus und erstellt finale Struktur.
    
    Args:
        dateien_kopieren (bool): True = Dateien kopieren, False = nur Simulation
    """
    zeige_schritt_header(
        4,
        "FINALIZE",
        "Wählt beste Sprachversionen aus und erstellt finale Struktur"
    )
    
    modus_text = "KOPIEREN AKTIVIERT" if dateien_kopieren else "SIMULATION (keine Dateiaktion)"
    print(f"Modus: {modus_text}")
    print(f"Quellordner: {FINAL_QUELLORDNER}")
    print(f"Zielordner: {FINAL_ZIELORDNER}")
    print(f"Sprach-Priorität: {' > '.join(SPRACH_PRIORITAET)}")
    print()
    
    quell_pfad = Path(FINAL_QUELLORDNER)
    ziel_pfad = Path(FINAL_ZIELORDNER)
    
    if not quell_pfad.exists():
        print(f"FEHLER: Quellordner '{FINAL_QUELLORDNER}' existiert nicht!")
        return
    
    # Verarbeite alle Plattform-Unterordner
    ergebnisse = []
    for unterordner in PLATTFORM_ORDNER:
        ergebnis = verarbeite_finalize_unterordner(
            quell_pfad,
            ziel_pfad,
            unterordner,
            dateien_kopieren
        )
        if ergebnis:
            ergebnisse.append(ergebnis)
    
    # Gesamtstatistik
    print(f"\n{'=' * SEPARATOR_LAENGE}")
    print("FINALIZE ABGESCHLOSSEN - GESAMTSTATISTIK")
    print(f"{'=' * SEPARATOR_LAENGE}")
    
    gesamt_dateien = sum(e['total'] for e in ergebnisse)
    gesamt_ausgewaehlt = sum(len(e['selected']) for e in ergebnisse)
    gesamt_entfernt = sum(len(e['removed']) for e in ergebnisse)
    
    print(f"\nGesamt über alle Plattformen:")
    print(f"  Dateien gesamt:  {gesamt_dateien}")
    print(f"  Ausgewählt:      {gesamt_ausgewaehlt}")
    print(f"  Entfernt:        {gesamt_entfernt}")
    
    if gesamt_dateien > 0:
        prozent = (gesamt_entfernt / gesamt_dateien) * 100
        print(f"  Einsparung:      {prozent:.1f}%")
    
    print("=" * SEPARATOR_LAENGE)

# ============================================================================
# HAUPTPROGRAMM - Workflow-Steuerung
# ============================================================================

def zeige_willkommen():
    """
    Zeigt Willkommens-Bildschirm mit Programminformationen.
    """
    print("\n" + "=" * SEPARATOR_LAENGE)
    print(" " * 15 + "WHDLoad Workflow 1.3")
    print("=" * SEPARATOR_LAENGE)
    print("\nDieses Programm führt 5 Schritte aus:")
    print("  1. FTP Mirror - Lädt Archive vom FTP-Server")
    print("  2. Sort - Sortiert nach Amiga-System (500/1200/CD32/CDTV)")
    print("  3. Compare - Vergleicht zwei Verzeichnisse")
    print("  4. Finalize - Wählt beste Sprachversionen aus")
    print("  5. Cleanup - Löscht temporäre Arbeitsordner")
    print("\nZwischen jedem Schritt werden Sie gefragt, ob Sie fortfahren möchten.")
    print("Alle Logs werden im Unterordner 'logs' gespeichert.")
    print("=" * SEPARATOR_LAENGE + "\n")

def fuehre_cleanup_aus():
    """
    Hauptfunktion für Schritt 5: Cleanup.
    Löscht temporäre Arbeitsordner nach Benutzerbestätigung.
    """
    zeige_schritt_header(
        5,
        "CLEANUP",
        "Löscht temporäre Arbeitsordner (whdload-mirror, whdload-sort)"
    )
    
    # Liste der zu löschenden Ordner mit Beschreibungen
    ordner_zum_loeschen = [
        (MIRROR_ORDNER, "whdload-mirror (FTP-Download-Cache)"),
        (SORT_ZIELORDNER, "whdload-sort (sortierte Archive)")
    ]
    
    geloescht = 0
    uebersprungen = 0
    
    # Gehe durch jeden Ordner und frage den Benutzer
    for ordner_pfad, beschreibung in ordner_zum_loeschen:
        if not os.path.exists(ordner_pfad):
            print(f"\n✓ Ordner '{ordner_pfad}' existiert nicht - übersprungen")
            continue
        
        # Berechne Ordnergröße für Info
        groesse_gb = berechne_ordner_groesse_gb(ordner_pfad)
        
        print(f"\n{'='*SEPARATOR_LAENGE}")
        print(f"Ordner: {beschreibung}")
        print(f"Pfad: {ordner_pfad}")
        print(f"Größe: {groesse_gb:.2f} GB")
        print('='*SEPARATOR_LAENGE)
        
        # Frage Benutzer, ob gelöscht werden soll (Standard: Ja)
        if benutzer_frage(f"Ordner '{ordner_pfad}' wirklich löschen?", "j"):
            try:
                shutil.rmtree(ordner_pfad)
                print(f"✓ Ordner '{ordner_pfad}' erfolgreich gelöscht ({groesse_gb:.2f} GB freigegeben)")
                geloescht += 1
            except Exception as e:
                print(f"✗ FEHLER beim Löschen von '{ordner_pfad}': {e}")
        else:
            print(f"→ Ordner '{ordner_pfad}' wird behalten")
            uebersprungen += 1
    
    # Zusammenfassung anzeigen
    print("\n" + "=" * SEPARATOR_LAENGE)
    print("CLEANUP ABGESCHLOSSEN - STATISTIK")
    print("=" * SEPARATOR_LAENGE)
    print(f"Gelöschte Ordner: {geloescht}")
    print(f"Behaltene Ordner: {uebersprungen}")
    print("=" * SEPARATOR_LAENGE)

def main():
    """
    Hauptfunktion - Orchestriert den kompletten Workflow.
    
    Führt alle 4 Schritte nacheinander aus mit Benutzerbestätigung
    zwischen den Schritten.
    """
    # Zeige Willkommen
    zeige_willkommen()
    
    # Setup Logging
    log_datei = erstelle_logdatei_namen("whdload_workflow")
    logger = DoppelLogger(log_datei)
    sys.stdout = logger
    
    try:
        print(f"Log-Datei: {log_datei}\n")
        print(f"Gestartet: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n")
        
        # ===== SCHRITT 1: FTP MIRROR =====
        if benutzer_frage("Schritt 1 (FTP Mirror) ausführen?", "j"):
            try:
                fuehre_ftp_mirror_aus()
            except Exception as e:
                print(f"\nFEHLER in Schritt 1: {e}")
                if not benutzer_frage("Trotzdem fortfahren?", "n"):
                    return
        else:
            print("Schritt 1 übersprungen.\n")
        
        # ===== SCHRITT 2: SORT =====
        if benutzer_frage("Schritt 2 (Sort) ausführen?", "j"):
            try:
                fuehre_sort_aus()
            except Exception as e:
                print(f"\nFEHLER in Schritt 2: {e}")
                if not benutzer_frage("Trotzdem fortfahren?", "n"):
                    return
        else:
            print("Schritt 2 übersprungen.\n")
        
        # ===== SCHRITT 3: COMPARE =====
        if benutzer_frage("Schritt 3 (Compare) ausführen?", "j"):
            try:
                fuehre_compare_aus()
            except Exception as e:
                print(f"\nFEHLER in Schritt 3: {e}")
                if not benutzer_frage("Trotzdem fortfahren?", "n"):
                    return
        else:
            print("Schritt 3 übersprungen.\n")
        
        # ===== SCHRITT 4: FINALIZE =====
        if benutzer_frage("Schritt 4 (Finalize) ausführen?", "j"):
            # Frage ob kopieren oder nur simulieren
            kopieren = benutzer_frage("Dateien wirklich kopieren? (Nein = nur Simulation)", "j")
            
            try:
                fuehre_finalize_aus(dateien_kopieren=kopieren)
            except Exception as e:
                print(f"\nFEHLER in Schritt 4: {e}")
        else:
            print("Schritt 4 übersprungen.\n")

        # ===== SCHRITT 5: CLEANUP =====
        if benutzer_frage("Schritt 5 (Cleanup) ausführen?", "j"):
            try:
                fuehre_cleanup_aus()
            except Exception as e:
                print(f"\nFEHLER in Schritt 5: {e}")
                import traceback
                traceback.print_exc()
        else:
            print("Schritt 5 übersprungen.\n")
        
        # ===== ABSCHLUSS =====
        print("\n" + "=" * SEPARATOR_LAENGE)
        print(" " * 20 + "WORKFLOW ABGESCHLOSSEN")
        print("=" * SEPARATOR_LAENGE)
        print(f"\nBeendet: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
        print(f"Log gespeichert: {log_datei}")
        print("=" * SEPARATOR_LAENGE + "\n")
        
    finally:
        # Cleanup: Logger schließen
        sys.stdout = logger.terminal
        logger.close()

# ============================================================================
# PROGRAMM-EINSTIEGSPUNKT
# ============================================================================

if __name__ == "__main__":
    """
    Wird ausgeführt wenn Skript direkt aufgerufen wird.
    """
    try:
        main()
    except KeyboardInterrupt:
        print("\n\nProgramm durch Benutzer abgebrochen (Strg+C)")
        sys.exit(0)
    except Exception as e:
        print(f"\n\nUnerwarteter Fehler: {e}")
        import traceback
        traceback.print_exc()
        sys.exit(1)
