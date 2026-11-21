"""Statische Exporte für gesammelte OPC-UA-Scandaten.

Erzeugt ausschließlich aus dem übergebenen Report-Wert (lokal gesammelte
Daten) zwei Dateien unter ``output_dir``:

* ``opcua_nodes.json``  – kanonisierte JSON-Abbildung der Rohdaten
* ``OPCUA_INTERFACE.md`` – nutzbare deutsche Markdown-Dokumentation

Es erfolgt KEIN Zugriff auf einen OPC-UA-Server. Alle Zeichenketten aus den
Scandaten gelten als unvertrauenswürdig und werden für Markdown geescaped
(inklusive HTML-, Tabellen-, Code- und Steuerzeichen), damit sie keine aktive
Formatierung erzeugen können. Fehlende Werte werden durchgängig als
"unbekannt" behandelt, niemals als leere Angaben.
"""

from __future__ import annotations

import json
from pathlib import Path

JSON_NAME = "opcua_nodes.json"
MD_NAME = "OPCUA_INTERFACE.md"
UNBEKANNT = "unbekannt"

# Sieben Themengruppen; Zuordnung nur per Bezeichnungs-/Beschreibungshinweis.
THEMEN = (
    ("Bewegung", ("beweg", "motion", "move", "achse", "axis", "robot", "arm", "gelenk", "joint")),
    ("Position", ("position", "pos", "koordinat", "coordinate", "target", "ziel")),
    ("Start/Stopp", ("start", "stop", "halt", "notfall", "emergency", "estop")),
    ("Programmverwaltung", ("programm", "program", "rezept", "recipe", "routine", "batch")),
    ("Status", ("status", "zustand", "state", "modus", "mode", "aktiv", "enabled")),
    ("Fehler", ("fehler", "error", "fault", "alarm", "warnung", "warning", "diagnose")),
    ("Bewegungsabläufe", ("ablau", "sequence", "sequenz", "schritt", "step", "phase", "faz")),
)

_ESCAPES = frozenset('\\`*_#+-![]()<>"\'|~')
_STEUER_ZU_LEER = ("\n", "\r", "\t")


def _md_escape(wert: object) -> str:
    """Macht einen beliebigen Wert als String für Markdown harmlos (keine aktive Formatierung)."""
    if wert is None:
        return UNBEKANNT
    text = str(wert)
    ausgabe = []
    for zeichen in text:
        code = ord(zeichen)
        if code < 0x20:
            if zeichen in _STEUER_ZU_LEER:
                ausgabe.append(" ")
            else:
                ausgabe.append("\\u{:04x}".format(code))
            continue
        if zeichen in _ESCAPES:
            ausgabe.append("\\" + zeichen)
        else:
            ausgabe.append(zeichen)
    return "".join(ausgabe)


def _anzeige(wert: object) -> str:
    """Anzeige eines optionalen Werts; fehlt/nul wird explizit 'unbekannt'."""
    if wert is None or wert == "":
        return UNBEKANNT
    return _md_escape(wert)


def _maske(wert: object) -> str:
    """Zugriffsmaske als Hex + Dezimal; nicht-integrale Werte = unbekannt."""
    if not isinstance(wert, int) or isinstance(wert, bool):
        return UNBEKANNT
    return "{:04x} ({})".format(wert, wert)


def _bit(wert: object, stelle: int) -> str:
    """Einzelnes Bit einer Maske (0=Read, 1=Write)."""
    if not isinstance(wert, int) or isinstance(wert, bool):
        return UNBEKANNT
    return "ja" if (wert >> stelle) & 1 else "nein"


def _index(nodes):
    """Node-Index keyed by node_id (erste Vorkommen gewinnt)."""
    by_id = {}
    for knoten in nodes:
        if not isinstance(knoten, dict):
            continue
        nid = knoten.get("node_id")
        if nid:
            by_id.setdefault(str(nid), knoten)
    return by_id


def _kanten_aufteilen(edges, node_ids):
    """Liefert (Nachkommen, Eingehend, Grenzkanten).

    Nachkommen: Parent -> [Child] innerhalb des erfassten Bereichs.
    Eingehend: Child -> [(Parent, RefType)] innerhalb des erfassten Bereichs.
    Grenzkanten: Kanten mit mindestens einem Endpunkt ausserhalb.
    """
    nachkommen, eingehend, grenz = {}, {}, []
    for kante in edges:
        if not isinstance(kante, dict):
            continue
        eltern, kind = kante.get("parent_id"), kante.get("child_id")
        if not eltern or not kind:
            continue
        eltern, kind = str(eltern), str(kind)
        if kind in node_ids:
            eingehend.setdefault(kind, []).append((eltern, kante.get("reference_type")))
        if eltern in node_ids:
            nachkommen.setdefault(eltern, []).append(kind)
        if eltern not in node_ids or kind not in node_ids:
            grenz.append((eltern, kind, kante.get("reference_type")))
    return nachkommen, eingehend, grenz


def _kanonischer_vater(nid, knoten, eingehend, by_id):
    """Kanonischer Elternteil: aufgezeichnete parent_id, sonst erste eingehende Kante."""
    vater = knoten.get("parent_id")
    if vater is not None and str(vater) in by_id:
        return str(vater)
    for eltern, _ in eingehend.get(nid, []):
        if eltern in by_id:
            return eltern
    return None


def _baumzeilen(root_id, by_id, nachkommen, eingehend):
    """Endliche Baumdarstellung (iterativer DFS, zyklus- und vertiefungsgeschützt)."""
    zeilen, besucht = [], set()
    stapel = [(root_id, 0)] if root_id else []
    while stapel:
        nid, tiefe = stapel.pop()
        if nid in besucht or nid not in by_id:
            continue
        besucht.add(nid)
        knoten = by_id[nid]
        beschriftung = _md_escape(knoten.get("browse_name") or knoten.get("node_id") or nid)
        klasse = _md_escape(knoten.get("node_class") or UNBEKANNT)
        vor = "  " * tiefe
        zeilen.append("{}- {} [{}] – NodeId: {}".format(vor, beschriftung, klasse, _md_escape(nid)))
        vater = _kanonischer_vater(nid, knoten, eingehend, by_id)
        alternativ = [
            "{} ({})".format(_md_escape(eltern), _anzeige(typ))
            for eltern, typ in eingehend.get(nid, [])
            if eltern != vater
        ]
        if alternativ:
            zeilen.append("{}  weitere eingehende Kanten: {}".format(vor, "; ".join(alternativ)))
        for kind in reversed(list(nachkommen.get(nid, []))):
            if kind not in besucht:
                stapel.append((kind, tiefe + 1))
    return zeilen, besucht


def _datentyp_anzeige(wert, by_id):
    """Datentyp-String plus Metadaten, falls ein zugehöriger Knoten erfasst ist."""
    if wert is None or wert == "":
        return UNBEKANNT
    typ = str(wert)
    knoten = by_id.get(typ)
    if knoten:
        name = knoten.get("display_name") or knoten.get("browse_name")
        if name:
            return "{} ({})".format(_md_escape(typ), _md_escape(name))
    return "{} – Datentyp-Knoten nicht erfasst".format(_md_escape(typ))


def _ist_component_kante(ref) -> bool:
    """HasComponent-Familie: passt auf die ns0-NodeId-Strings von
    HasComponent (47) und HasOrderedComponent (49) in der von
    ``NodeId.to_string()`` erzeugten kanonischen Form (``i=47``; bei
    NamespaceIndex 0 wird kein ``ns=``-Präfix geschrieben), nicht auf Namen."""
    ref = str(ref or "")
    return ref in ("i=47", "i=49")


def _paramtabelle(args, by_id):
    """Markdown-Tabelle für Methoden-Argumentlisten."""
    zeilen = [
        "| Name | Datentyp | Rank | Dimensionen | Beschreibung |",
        "| --- | --- | --- | --- | --- |",
    ]
    for a in args:
        if not isinstance(a, dict):
            continue
        dims = a.get("array_dimensions")
        if dims is None:
            dim_txt = UNBEKANNT
        elif isinstance(dims, (list, tuple)):
            dim_txt = _md_escape(",".join(str(d) for d in dims)) if dims else "[]"
        else:
            dim_txt = _md_escape(dims)
        zeilen.append("| {} | {} | {} | {} | {} |".format(
            _anzeige(a.get("name")),
            _datentyp_anzeige(a.get("data_type"), by_id),
            _anzeige(a.get("value_rank")),
            dim_txt,
            _anzeige(a.get("description")),
        ))
    return zeilen


def _render_markdown(report: dict) -> str:
    """Baue die komplette deutsche Markdown-Dokumentation aus dem Report."""
    nodes = report.get("nodes") or []
    edges = report.get("edges") or []
    root_id = str(report.get("root") or "")
    limits = report.get("limits") or {}
    issues = [str(i) for i in (report.get("issues") or [])]
    komplett = bool(report.get("complete"))

    by_id = _index(nodes)
    nachkommen, eingehend, grenz = _kanten_aufteilen(edges, set(by_id))
    baumzeilen, erreicht = _baumzeilen(root_id, by_id, nachkommen, eingehend)

    klassen = {}
    for knoten in nodes:
        if not isinstance(knoten, dict):
            continue
        klasse = str(knoten.get("node_class") or UNBEKANNT)
        klassen[klasse] = klassen.get(klasse, 0) + 1

    t = []
    t.append("# OPC-UA-Schnittstelle – Auswertung der Scandaten")
    t.append("")
    t.append("_Erzeugt rein statisch aus lokal gesammelten Scandaten; es erfolgte "
             "kein Live-Zugriff auf einen OPC-UA-Server. Fehlende Werte sind als "
             "„unbekannt“ markiert._")
    t.append("")

    # ── 1. Überblick ─────────────────────────────────────────────────
    t.append("## 1. Überblick")
    t.append("")
    t.append("- Erfasste Knoten: {}".format(len(nodes)))
    t.append("- Erfasste Kanten: {}".format(len(edges)))
    t.append("- Knoten nach Klasse:")
    for klasse in sorted(klassen):
        t.append("  - {}: {}".format(_md_escape(klasse), klassen[klasse]))
    t.append("")
    t.append("**Limits (aus Scan-Metadaten):**")
    if limits:
        for schlüssel in sorted(limits, key=str):
            t.append("- {}: {}".format(_md_escape(schlüssel), _anzeige(limits[schlüssel])))
    else:
        t.append("- {}".format(UNBEKANNT))
    t.append("")
    t.append("**Scan vollständig:** {}".format("ja" if komplett else "nein"))
    t.append("")

    # ── 2. Hierarchie (endliche Ansicht) ─────────────────────────────
    t.append("## 2. Hierarchie (endliche Ansicht)")
    t.append("")
    t.append("Jeder Knoten erscheint an seiner kanonischen Elternstelle; "
             "zusätzliche eingehende Kanten (gemeinsame Nutzung, Zyklen) werden "
             "am Knoten vermerkt.")
    t.append("")
    if root_id and root_id in by_id:
        t.extend(baumzeilen)
    else:
        t.append("Der Wurzel-Knoten ({}) wurde nicht in den erfassten Knoten "
                 "gefunden.".format(_md_escape(root_id) or UNBEKANNT))
    t.append("")
    unerreichbar = sorted(nid for nid in by_id if nid not in erreicht)
    if unerreichbar:
        t.append("**Nicht über die Wurzelhierarchie erreichbar "
                 "(mögliche Zyklen oder getrennte Teilbäume):**")
        for nid in unerreichbar:
            t.append("- {}".format(_md_escape(nid)))
        t.append("")
    if grenz:
        t.append("**Kanten außerhalb des erfassten Bereichs (Grenz-/Wurzelkanten):**")
        for eltern, kind, typ in grenz:
            t.append("- {} → {} ({})".format(_md_escape(eltern), _md_escape(kind), _anzeige(typ)))
        t.append("")

    # ── 3. Variablen: Metadaten und Rechte ───────────────────────────
    t.append("## 3. Variablen – Metadaten und Rechte")
    t.append("")
    variablen = [n for n in nodes if isinstance(n, dict) and n.get("node_class") == "Variable"]
    if variablen:
        t.append("| NodeId | BrowseName | DisplayName | Datentyp | Rank | AccessLevel | UserAccessLevel | Read (A) | Write (A) | Read (U) | Write (U) |")
        t.append("| --- |" + " --- |" * 10)
        for n in sorted(variablen, key=lambda k: str(k.get("node_id") or "")):
            t.append("| {} | {} | {} | {} | {} | {} | {} | {} | {} | {} | {} |".format(
                _md_escape(n.get("node_id")),
                _anzeige(n.get("browse_name")),
                _anzeige(n.get("display_name")),
                _datentyp_anzeige(n.get("data_type"), by_id),
                _anzeige(n.get("value_rank")),
                _maske(n.get("access_level")),
                _maske(n.get("user_access_level")),
                _bit(n.get("access_level"), 0),
                _bit(n.get("access_level"), 1),
                _bit(n.get("user_access_level"), 0),
                _bit(n.get("user_access_level"), 1),
            ))
        t.append("")
        t.append("Read/Write geben Bit 0 bzw. Bit 1 der gemessenen Masken wieder "
                 "(AccessLevel bzw. UserAccessLevel). Sie dokumentieren den "
                 "gemessenen Zustand; daraus lässt sich keine wirksame "
                 "Sicherheit- oder Berechtigungsstellung ableiten.")
    else:
        t.append("Es wurden keine Variablenknoten erfasst.")
    t.append("")

    # ── 4. Methoden ──────────────────────────────────────────────────
    t.append("## 4. Methoden")
    t.append("")
    methoden = [n for n in nodes if isinstance(n, dict) and n.get("node_class") == "Method"]
    if not methoden:
        t.append("Es wurden keine Methodenknoten erfasst.")
        t.append("")
    for n in sorted(methoden, key=lambda k: str(k.get("node_id") or "")):
        mid = str(n.get("node_id") or "")
        t.append("### {}".format(_md_escape(n.get("browse_name") or mid or UNBEKANNT)))
        t.append("")
        t.append("NodeId: {}".format(_md_escape(mid) if mid else UNBEKANNT))
        t.append("")
        if n.get("description"):
            t.append("Beschreibung: {}".format(_md_escape(n.get("description"))))
            t.append("")
        for titel, schlüssel in (("Eingabeparameter", "input_arguments"),
                                 ("Ausgabeparameter", "output_arguments")):
            t.append("**{}:**".format(titel))
            args = n.get(schlüssel)
            if args:
                t.extend(_paramtabelle(args, by_id))
            else:
                t.append("- {}".format(UNBEKANNT))
            t.append("")
        owner = sorted({
            str(e.get("parent_id"))
            for e in edges
            if isinstance(e, dict)
            and str(e.get("child_id") or "") == mid
            and _ist_component_kante(e.get("reference_type"))
            and str(e.get("parent_id")) in by_id
            and by_id[str(e.get("parent_id"))].get("node_class") in ("Object", "ObjectType")
        })
        t.append("**Bekannte referenzierende Owner (HasComponent-Familie, Object/ObjectType):**")
        if owner:
            for oid in owner:
                ok = by_id[oid]
                t.append("- {} [{}] – NodeId: {}".format(
                    _md_escape(ok.get("browse_name") or oid),
                    _md_escape(ok.get("node_class") or UNBEKANNT),
                    _md_escape(oid),
                ))
        else:
            t.append("- Keine bekannten HasComponent-Kanten im Scan; mögliche Owner "
                     "sind nicht verifiziert.")
        t.append("")
        typen = sorted({
            str(a.get("data_type"))
            for a in (n.get("input_arguments") or []) + (n.get("output_arguments") or [])
            if isinstance(a, dict) and a.get("data_type")
        })
        if typen:
            t.append("**Referenzierte Datentypen:**")
            for typ in typen:
                t.append("- {}".format(_datentyp_anzeige(typ, by_id)))
            t.append("")

    # ── 5. Themenzuordnung (Hypothesen) ──────────────────────────────
    t.append("## 5. Themenzuordnung (Hypothesen)")
    t.append("")
    t.append("Zuordnung ausschließlich anhand von Bezeichnungen und "
             "Beschreibungen. Die Einträge sind Hypothesen ohne semantische "
             "Bestätigung; es werden keinerlei konkrete Befehle, Verfahren oder "
             "SPS-Inhalte abgeleitet.")
    t.append("")
    for thema, schluessel in THEMEN:
        t.append("### {}".format(thema))
        treffer = []
        for knoten in nodes:
            if not isinstance(knoten, dict):
                continue
            text = " ".join(
                str(knoten.get(feld) or "")
                for feld in ("browse_name", "display_name", "description")
            ).lower()
            for kw in schluessel:
                if kw in text:
                    treffer.append((knoten, kw))
                    break
        if treffer:
            for knoten, kw in treffer:
                t.append("- {} [{}]: Indiz „{}“".format(
                    _md_escape(knoten.get("browse_name") or knoten.get("node_id") or UNBEKANNT),
                    _md_escape(knoten.get("node_class") or UNBEKANNT),
                    _md_escape(kw),
                ))
        else:
            t.append("- Keine Indizien in den erfassten Bezeichnungen/Beschreibungen.")
        t.append("")

    # ── 6. Offene Fragen und Scan-Lücken ─────────────────────────────
    t.append("## 6. Offene Fragen und Scan-Lücken")
    t.append("")
    fragen = list(issues)
    if not komplett:
        fragen.append("Scan laut Metadaten nicht vollständig; es können weitere Daten fehlen.")
    if grenz:
        fragen.append("{} Kanten liegen außerhalb des erfassten Knotenbereichs.".format(len(grenz)))
    if unerreichbar:
        fragen.append("{} Knoten sind über die Wurzelhierarchie nicht erreichbar.".format(len(unerreichbar)))
    ohne_beschreibung = sum(1 for k in nodes if isinstance(k, dict) and not k.get("description"))
    if ohne_beschreibung:
        fragen.append("{} Knoten tragen keine Beschreibung.".format(ohne_beschreibung))
    if not fragen:
        fragen.append("Keine offenen Fragen dokumentiert.")
    for frage in fragen:
        t.append("- {}".format(_md_escape(frage)))
    t.append("")
    return "\n".join(t)


def write_reports(report: dict, output_dir: Path) -> None:
    """Schreibt ``opcua_nodes.json`` und ``OPCUA_INTERFACE.md`` nach ``output_dir``.

    ``report`` enthält die gesammelten Scandaten (Root-NodeId, Knotenliste,
    Kantenliste, Limits, Issues, Vollständigkeit). Es findet keinerlei
    Kommunikation mit einem OPC-UA-Server statt; alle Ausgaben entstehen
    ausschließlich aus dem übergebenen Wert.
    """
    ziel = Path(output_dir)
    ziel.mkdir(parents=True, exist_ok=True)
    roh = json.dumps(report, indent=2, sort_keys=True, ensure_ascii=False)
    (ziel / JSON_NAME).write_text(roh + "\n", encoding="utf-8")
    (ziel / MD_NAME).write_text(_render_markdown(report), encoding="utf-8")
