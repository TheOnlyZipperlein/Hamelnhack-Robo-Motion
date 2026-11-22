# OPC-UA-Interfacespezifikation – Robotiksteuerung (c500)

## 1. Zweck und Geltungsbereich

Diese Spezifikation beschreibt die OPC-UA-Schnittstelle der SPS (`c500`) für
die Entwicklung von **Robotiksteuerungssoftware**, die über OPC UA mit der
Anlage kommuniziert. Sie ist die konsolidierte Referenz aus drei
ausschließlich lesefähigen Metadatenscans (Details: Abschnitt 9).

| | |
| --- | --- |
| Endpunkt | `opc.tcp://192.168.5.99:4840` |
| Vollständig dokumentiert | der gesamte Zweig `GVL_Exchange` (143 Knoten, 142 Referenzen, Scan vollständig, Exit-Code 0) |
| Teilweise dokumentiert | der übrige Adressraum (1500-Knoten-Lauf, unvollständig; Abschnitt 8) |
| Gelesen wurde | ausschließlich Metadaten (Namen, Klassen, Datentypen, ValueRank, Zugriffsrechte, Beschreibungen) und Methodensignaturen |
| **Nicht** gelesen | Prozesswerte, Steuerbefehle, Methodenaufrufe, Schreiboperationen |

**Wichtig für die Implementierung:** Die Rollenzuordnungen in Abschnitt 3
(„Befehl“, „Status“, „Soll“, „Ist“) sind **Hypothesen aus der Namenskongruenz**
nach IEC-61131-Übung. Sie sind vom Server nicht dokumentiert und müssen vor
dem Produktiveinsatz gegen das SPS-Programm bzw. die Anlagendokumentation
verifiziert werden. Aus dieser Spezifikation lassen sich **keine**
Betriebs- oder Sequenzierbefehle ableiten; es werden bewusst keine
Bedienvorschriften abgeleitet.

## 2. Der Austauschzweig `GVL_Exchange`

`GVL_Exchange` ist eine globale Variablenliste (GVL) und der zentrale
Austauschbereich zwischen SPS und externer Steuerung.

| | |
| --- | --- |
| Pfad im Adressraum | `Objects → DeviceSet → c500 → Resources → Application → GlobalVars → GVL_Exchange` |
| NodeId der Wurzel | `ns=4;s=\|appo\|c500.Application.GVL_Exchange` |
| Direkte Mitglieder | 59 (1 Array + 58 Skalare) |
| Gesamtknoten | 143 (alle `Variable`), 142 `Organizes`-Referenzen |
| Zugriff | 139 Knoten lesen/schreiben (Maske `3`); 4 nur lesbar (`1`): die Wurzel selbst sowie `Dimensions`, `IndexMin`, `IndexMax` direkt unter `g_ascProfilePar` |

Observation, die bei der Implementierung beachtet werden muss: **Alle**
Austauschvariablen sind aus Sicht des Clients schreibbar (auch die
mutmaßlichen Statussignale). Aus den Zugriffsrechten lässt sich **nicht**
ableiten, wer welche Variable wann schreibt – das ergibt sich nur aus dem
Handshake-Protokoll der Anlage, das in dieser Spezifikation nicht enthalten
ist.

## 3. Variablengruppen und mutmaßliche Rollen

> Alle Rollen-Zuordnungen unten sind **Hypothesen** (Namenskongruenz,
> IEC-61131-Prefixe: `g_` global, `x` Bool, `w` Word, `lr` großes
> Real/Ganze Zahl). Nicht verifiziert.

### 3.1 Bewegungsprofile `g_ascProfilePar` (Array)

| Variable | Datentyp | ValueRank | Zugriff | Mutmaßliche Rolle |
| --- | --- | --- | --- | --- |
| `g_ascProfilePar` | `ns=4;s=\|type\|c500.Application.scProfilePar` | 1 (1-dim.) | 3 (ls/rs) | Profilparameter-Bank, 10 Profile (Index 1–10) |

Jedes der 10 Elemente trägt denselben Strukturtyp `scProfilePar` mit 7
Gliedern (Abschnitt 4). Die Profile tragen für jede Bahn mutmaßlich
Positions- und Dynamikparameter (X/Z-Position, Bahngeschwindigkeit,
-Beschleunigung, -Dezeleration, Blendradius) plus einen Halt-Flag.

### 3.2 Soll-Werte

| Variable | Datentyp | ValueRank | Zugriff | Mutmaßliche Rolle |
| --- | --- | --- | --- | --- |
| `g_lrSetValue0` … `g_lrSetValue7` | `i=11` (Double) | -1 | 3 | Sollwerte für 8 Kanäle/Achsen (Index 0–7) |
| `g_lrOverride` | `i=11` (Double) | -1 | 3 | Override-Faktor (z. B. Taster-Override) |
| `g_lrSetPathVelOut` | `i=11` (Double) | -1 | 3 | Bahngeschwindigkeits-Sollwert (Ausgang) |

### 3.3 Ist-Werte

| Variable | Datentyp | ValueRank | Zugriff | Mutmaßliche Rolle |
| --- | --- | --- | --- | --- |
| `g_lrActValue0` … `g_lrActValue7` | `i=11` (Double) | -1 | 3 | Istwerte für 8 Kanäle/Achsen |
| `g_lrActXPos` | `i=11` (Double) | -1 | 3 | Ist-X-Position |
| `g_lrActXVel` | `i=11` (Double) | -1 | 3 | Ist-X-Geschwindigkeit |
| `g_lrActZPos` | `i=11` (Double) | -1 | 3 | Ist-Z-Position |
| `g_lrActZVel` | `i=11` (Double) | -1 | 3 | Ist-Z-Geschwindigkeit |

### 3.4 Steuer-, Status- und Fehlerworte

| Variable | Datentyp | ValueRank | Zugriff | Mutmaßliche Rolle |
| --- | --- | --- | --- | --- |
| `g_wCtrl0` … `g_wCtrl7` | `ns=3;i=3002` (unbekannter Benutzertyp) | -1 | 3 | Steuerworte (je Bit ein Einzelbefehl, mutmaßlich) |
| `g_wStat1` … `g_wStat7` | `ns=3;i=3002` | -1 | 3 | Statusworte (je Bit ein Einzelstatus, mutmaßlich) |
| `g_wErrorID` | `ns=3;i=3002` | -1 | 3 | Fehlerkennung |

Der Typ `ns=3;i=3002` stammt aus Namespace 3, das in keinem der Scans
erfasst wurde; die genaue Typdefinition (Bits, Engineering-Einheit) ist
**offen** (Abschnitt 7).

### 3.5 Befehlssignale (Boolesche, mutmaßlich vom Roboter zu schreiben)

| Variable | Datentyp | Zugriff | Mutmaßliche Rolle |
| --- | --- | --- | --- |
| `g_xPathExecute` | `i=1` (Boolean) | 3 | Bahnlauf starten |
| `g_xPathHalt` | `i=1` | 3 | Bahnlauf anhalten |
| `g_xPathHold` | `i=1` | 3 | Halten (Hold) |
| `g_xPathStop` | `i=1` | 3 | Stopp |
| `g_xPathInterrupt` | `i=1` | 3 | Unterbrechen |
| `g_xHomeExecute` | `i=1` | 3 | Homing-Prozedur starten |
| `g_xManJogXPos` / `g_xManJogXNeg` | `i=1` | 3 | Manuell joggen X +/- |
| `g_xManJogZPos` / `g_xManJogZNeg` | `i=1` | 3 | Manuell joggen Z +/- |
| `g_xPulseGripper` | `i=1` | 3 | Greifer-Impuls |
| `g_xErrorReset` | `i=1` | 3 | Fehler zurücksetzen |
| `g_xMotorBrakeRelease` | `i=1` | 3 | Motorbremse freigeben |

### 3.6 Statussignale (Boolesche, mutmaßlich vom Roboter zu lesen)

| Variable | Datentyp | Zugriff | Mutmaßliche Rolle |
| --- | --- | --- | --- |
| `g_xDone` | `i=1` | 3 | Vorgang abgeschlossen |
| `g_xError` | `i=1` | 3 | Fehler aktiv |
| `g_xWarning` | `i=1` | 3 | Warnung aktiv |
| `g_xAckPathHalt` | `i=1` | 3 | Halt quittiert |
| `g_xHomePosAvailable` | `i=1` | 3 | Home-Position verfügbar |
| `g_xMotorBrakseReleased` | `i=1` | 3 | Bremse freigegeben – **Tippfehler in der Anlage** (fehlt das `e`); exakt so ansprechen |
| `g_xGripperCtrlActive` | `i=1` | 3 | Greifersteuerung aktiv |

## 4. Struktur `scProfilePar`

Jedes Arrayelement `g_ascProfilePar[n]` (n = 1…10) enthält:

| Glied | Datentyp | ValueRank | Zugriff | Mutmaßliche Bedeutung |
| --- | --- | --- | --- | --- |
| `lrXPos` | `i=11` (Double) | -1 | 3 | X-Position des Profils |
| `lrZPos` | `i=11` (Double) | -1 | 3 | Z-Position des Profils |
| `lrPathVel` | `i=11` (Double) | -1 | 3 | Bahngeschwindigkeit |
| `lrPathAcc` | `i=11` (Double) | -1 | 3 | Beschleunigung |
| `lrPathDec` | `i=11` (Double) | -1 | 3 | Dezeleration |
| `lrBlendingRadius` | `i=11` (Double) | -1 | 3 | Blendradius |
| `xHalt` | `i=1` (Boolean) | -1 | 3 | Halt-Flag des Profils |

Direkt unter `g_ascProfilePar` (nicht in den Elementen) liegen drei nur
lesbare Array-Metadaten: `Dimensions` (`i=7`, UInt32), `IndexMin` (`i=6`,
Int32), `IndexMax` (`i=6`, Int32) – Länge und Indizierungsbereich des Arrays.

## 5. Hierarchie und NodeIds

Komplette Hierarchie des Zweigs (143 Knoten):

```
- `GVL_Exchange` [Variable]
  - `g_ascProfilePar` [Variable]
    - `Dimensions` [Variable]
    - `IndexMin` [Variable]
    - `IndexMax` [Variable]
    - `g_ascProfilePar[1]` … `g_ascProfilePar[10]` [Variable]  (je 7 Glieder)
      - `lrXPos`, `lrZPos`, `lrPathVel`, `lrPathAcc`, `lrPathDec`,
        `lrBlendingRadius`, `xHalt` [Variable]
  - `g_lrActValue0` … `g_lrActValue7` [Variable]
  - `g_lrActXPos`, `g_lrActXVel`, `g_lrActZPos`, `g_lrActZVel` [Variable]
  - `g_lrOverride`, `g_lrSetPathVelOut` [Variable]
  - `g_lrSetValue0` … `g_lrSetValue7` [Variable]
  - `g_wCtrl0` … `g_wCtrl7`, `g_wStat1` … `g_wStat7`, `g_wErrorID` [Variable]
  - `g_xAckPathHalt`, `g_xDone`, `g_xError`, `g_xErrorReset`,
    `g_xGripperCtrlActive`, `g_xHomeExecute`, `g_xHomePosAvailable`,
    `g_xManJogXNeg`, `g_xManJogXPos`, `g_xManJogZNeg`, `g_xManJogZPos`,
    `g_xMotorBrakeRelease`, `g_xMotorBrakseReleased`, `g_xPathExecute`,
    `g_xPathHalt`, `g_xPathHold`, `g_xPathInterrupt`, `g_xPathStop`,
    `g_xPulseGripper`, `g_xWarning` [Variable]
```

NodeId-Schema (vollständige Liste in `out/run-20261121-172846/`):

| Muster | Beispiel | Bedeutung |
| --- | --- | --- |
| `ns=4;s=\|appo\|…` | `ns=4;s=\|appo\|c500.Application.GVL_Exchange` | Objekt-Knoten der Anwendung |
| `ns=4;s=\|var\|…` | `ns=4;s=\|var\|c500.Application.GVL_Exchange.g_xDone` | Variablen-Knoten der Anwendung |
| `ns=4;s=\|type\|…` | `ns=4;s=\|type\|c500.Application.scProfilePar` | Typdefinition der Anwendung |
| `ns=3;i=3002` | Datentyp der `g_w*`-Worte | Benutzertyp, Namespace 3 (nicht erfasst) |
| `i=<nr>` | `i=1`, `i=6`, `i=7`, `i=11` | Standarddatentypen (Namespace 0) |

## 6. Lesehilfe

- **Datentypen (Namespace 0):** `i=1` = Boolean, `i=6` = Int32, `i=7` =
  UInt32, `i=11` = Double. Alle `lr*`-Werte sind Double – die skalierte
  physikalische Einheit (z. B. mm, µm, mm/s) ist aus den Metadaten **nicht**
  ersichtlich und muss aus der Anlagendokumentation übernommen werden.
- **ValueRank:** `-1` = Skalar, `0` = beliebige Dimension, `1` =
  eindimensional. Alle GVL-Mitglieder sind Skalare außer `g_ascProfilePar`
  (Rank 1, Index 1–10).
- **AccessLevel:** Bitmaske, Bit 0 = lesen, Bit 1 = schreiben. `3` =
  lesen/schreiben, `1` = nur lesen. `UserAccessLevel` ist der für die
  aktuelle (anonyme) Sitzung zugeordnete Wert – hier identisch.
- **Referenzen:** sämtliche 142 Kanten sind `Organizes` (hierarchisch, ns0:i=35).

## 7. Offene Punkte und Grenzen

1. **Semantik nicht verifiziert:** Rollen-Zuordnungen (Abschnitt 3) stammen
   aus Namen; Verhalten, Einheiten, Reihenfolgen und Quittier-Protokolle sind
   aus Metadaten nicht ableitbar.
2. **Typ `ns=3;i=3002`** (alle `g_w*`-Worte): Definition unbekannt,
   Namespace 3 wurde nicht gescannt.
3. **Skalierung/Einheiten:** Double-Werte ohne Units-Metadaten; Faktoren
   (z. B. 0,1 mm pro LSB) sind anlagenseitig zu klären.
4. **Handshake/Timing:** Aus der Schnittstelle geht nicht hervor, wie
   Befehle zu quittieren sind (Edge- vs. Level-Signale), welche Zykluszeiten
   gelten und in welcher Folge Homing/Bahnlauf abzulaufen haben.
5. **Rest des Adressraums:** nur teilweise bekannt (Abschnitt 8); weitere
   GVLs oder programmnahe Schnittstellen könnten existieren.
6. **Keine Betriebsvorschrift:** Diese Doku ist eine Datenbeschreibung; sie
   ersetzt weder die Anlagendokumentation noch eine Inbetriebnahme mit dem
   Verantwortlichen.

## 8. Appendix A: Adressraum jenseits von `GVL_Exchange`

Aus dem breiten 1500-Knoten-Lauf (unvollständig, 21.11.2026, Tiefe bis 7):

**Top-Level unter `Objects`** (7 Objekte): `Locations`, `Server`, `Aliases`,
`DeviceSet`, `NetworkSet`, `DeviceTopology`, `Filesystem`.

**Anlagenzweig:**

```
Objects → DeviceSet → c500 → Resources → Application
    ├─ Tasks [Object]
    ├─ Programs [Object]
    ├─ Manufacturer, Model, HardwareRevision, SoftwareRevision,
    │  DeviceRevision, DeviceManual, SerialNumber, RevisionCounter [Variable]
    └─ GlobalVars [Object]
         └─ GVL_Exchange [Object]   ← einziger GVL in diesem Lauf
```

**Statistik des breiten Laufs:** 1500 Knoten (171 Object, 1031 Variable,
298 Method), Tiefe-Verteilung 1/7/33/122/299/514/523/1; Lauf unvollständig
(Knoten-Limit erreicht).

**Methoden (298):** sämtlich **standardmäßige OPC-UA-Serverdienste**, keine
anlagenbezogene Logik erkennbar:

| Kategorie | Methoden (Anzahl im Lauf) |
| --- | --- |
| Dateisystem/Storage | `Open`, `Close`, `Read`, `Write`, `GetPosition`, `SetPosition`, `CreateDirectory`, `CreateFile`, `Delete`, `MoveOrCopy` (≈257, verteilt über `Filesystem`, `Locations`, `user_data`, `sdcard`) |
| Alias | `FindAlias` (3) |
| Subscription | `GetMonitoredItems`, `ResendData`, `SetSubscriptionDurable` |
| Sicherheit | `GetSecurityKeys`, `SetSecurityKeys`, `GetSecurityGroup`, `AddSecurityGroup`, `RemoveSecurityGroup`, `ChangePassword`, `AddPushTarget`, `RemovePushTarget` |
| Serververwaltung | `AddIdentity`, `RemoveIdentity`, `AddApplication`, `RemoveApplication`, `AddEndpoint`, `RemoveEndpoint`, `RequestTickets` |

**Namespaces** (Knotenanteil im Lauf): `ns=10` = 820 (virtuelles
Dateisystem), `ns=1` = 268, `ns=9` = 41, `ns=4` = 24 (Anwendung c500),
`ns=2` = 17, `ns=3` = 11.

## 9. Appendix B: Scan-Läufe und Reproduzierbarkeit

| Lauf (Ordner unter `out/`) | Parameter | Ergebnis |
| --- | --- | --- |
| `run-20261121-164213` | Standardwurzel `Objects`, Tiefe 2, 50 Knoten | 41 Knoten, Exit 2 (Tiefenlimit) |
| `run-20261121-171544` | Standardwurzel, Tiefe 8, 1500 Knoten, 4000 Anfragen | 1500 Knoten, Exit 2 (Knotenlimit) |
| `run-20261121-172846` | `--root` GVL_Exchange, Tiefe 6, 400 Knoten | **143 Knoten, Exit 0, vollständig** |

Reproduktion (aus dem Projektordner, nur Leseoperationen):

```powershell
# Vollständiger Scan des Austauschzweigs:
.\.venv\Scripts\python.exe -B opcua-doc\main.py `
  --security-policy None --allow-insecure `
  --root "ns=4;s=|appo|c500.Application.GVL_Exchange" `
  --max-depth 6 --max-nodes 400 --max-requests 800 `
  --page-size 20 --request-interval 0.15 --timeout 5 --scan-timeout 300 `
  --output-dir .\opcua-doc\out\run-neu
```

Die Rohdaten (`opcua_nodes.json`, generiertes `OPCUA_INTERFACE.md`, die
Detailreferenz `OPCUA_TECHNISCHE_DOKUMENTATION.md`) liegen unter
`opcua-doc/out/` und sind per `.gitignore` aus dem Repository ausgeschlossen.
Diese Datei ist die einzig versionierte Interfacespezifikation.
