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