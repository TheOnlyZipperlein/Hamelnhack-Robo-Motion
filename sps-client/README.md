# SPS-Client – Robotiksteuerung über OPC UA (`sps-client`)

Dieses Modul ist der **SPS-Client** des Projekts: Eine Python-Library, die
die OPC-UA-Schnittstelle der SPS (`c500`, Zweig `GVL_Exchange`) in
semantische **Wrapper-Funktionen** für die Robotiksteuerung übersetzt. Die
künftige Web-App (Flask) ruft diese Funktionen auf; über die Web-App wird
der Roboter von einem AI-Agenten gesteuert.

```
AI-Agent → Web-App (Flask, geplant) → sps_client (dieses Modul) → OPC UA → SPS (c500)
```

Die Variablenspezifikation (NodeIds, Datentypen, Zugriffsrechte) stammt aus
den lesefähigen Scans in [`../opcua-doc/`](../opcua-doc/) – verbindliche
Referenz: [`OPCUA_ROBOT_INTERFACE.md`](../opcua-doc/OPCUA_ROBOT_INTERFACE.md).

## 1. Voraussetzungen

- Python **≥ 3.11**
- `asyncua==2.1.0` – siehe [`requirements.txt`](./requirements.txt)
  (liegt bereits in der Projektumgebung `.venv`)
- Für Live-Betrieb: Erreichbarkeit des OPC-UA-Endpunkts
  (Standard: `opc.tcp://192.168.5.99:4840`, konfiguriert ohne Zertifikat)

Installation (falls die Projekt-Umgebung nicht reicht):

```powershell
.\.venv\Scripts\python.exe -m pip install -r sps-client\requirements.txt
```

## 2. Aufbau

| Datei | Rolle |
| --- | --- |
| `sps_client/config.py` | `PlcConfig` – Einstellungen, komplett umgebungsvariablengetrieben |
| `sps_client/nodes.py` | NodeId-Konstanten aller `GVL_Exchange`-Variablen |
| `sps_client/plc.py` | `PlcClient` – persistente Session, Low-Level Read/Write, Reconnect, Write-Gate; `FakeBackend` (Dry-Run) |
| `sps_client/robot.py` | `Robot` – semantische Wrapper (Bewegung, Profile, Status, Warte-Helfer) |
| `sps_client/bridge.py` | `SyncPlc` – blockierende Fassade für synchrone Frameworks (Flask) |
| `main.py` | CLI zum Testen: `ping`, `status`, `positions`, `profiles`, `demo` |

## 3. CLI-Nutzung

Aus dem Projektordner (Windows PowerShell):

```powershell
# Verbindungstest ohne Anlage (Fake-PLC):
.\.venv\Scripts\python.exe sps-client\main.py ping --dry-run

# Status-Snapshot (einmalig bzw. fortlaufend):
.\.venv\Scripts\python.exe sps-client\main.py status --dry-run
.\.venv\Scripts\python.exe sps-client\main.py status --dry-run --watch --interval 0.5

# Soll/Ist-Werte und die 10 Bewegungsprofile:
.\.venv\Scripts\python.exe sps-client\main.py positions --dry-run
.\.venv\Scripts\python.exe sps-client\main.py profiles --dry-run

# Geskripteter Vollzyklus (Homing, Profil, Bahnlauf, Gripper, Brems, Jog):
.\.venv\Scripts\python.exe sps-client\main.py demo

# Live-Anlage (read-only, ohne Schreibfreigabe):
.\.venv\Scripts\python.exe sps-client\main.py status
```

## 4. Nutzung als Library

### 4.1 Asynchron (empfohlener Basisstil)

```python
from sps_client import PlcConfig, Robot

config = PlcConfig.from_env(write_enabled=True)  # Schreiben explizit freigegeben
async with Robot(config) as bot:
    bot.set_profile(1, x_pos=1000, z_pos=500, path_vel=100)
    await bot.start_path(1)
    if not await bot.wait_done(timeout=30):
        ...  # Eskalation: Alarm, Abbruch, Retry – Entscheidung der App
```

### 4.2 Synchrom / Flask

`SyncPlc` kapselt die Event-Loop-Thread; alle `Robot`-Methoden werden
blockierend:

```python
from sps_client import PlcConfig, SyncPlc

plc = SyncPlc(PlcConfig.from_env())          # einmal beim App-Start

@app.route("/status")
def status():
    s = plc.status()
    return {"done": s.done, "error": s.error, "warning": s.warning}

@app.route("/move", methods=["POST"])
def move():
    plc.start_path(1)
    return {"started": True}

# Beim Shutdown:
plc.close()
```

### 4.3 Funktionsübersicht (`Robot`)

| Funktion | Wirkung (GVL-Variablen) |
| --- | --- |
| `status()` | Snapshot aller Statussignale + Wort (1 Batch-Read) |
| `positions()` | Snapshot aller Soll/Ist-Werte (1 Batch-Read) |
| `get_profile(i)` / `set_profile(i, **felder)` | Lesen/Teil-Schreiben von `g_ascProfilePar[i]` (i = 1..10) |
| `start_path(i)` | Bahnlauf starten (`g_xPathExecute`; optional `g_lrSetPathVelOut`, `g_lrOverride` vorab) |
| `halt_path()` / `hold_path()` / `stop_path()` / `interrupt_path()` | Bahnlauf-Stoppvarianten |
| `home()` | Homing starten (`g_xHomeExecute`) |
| `jog(axis, positive, enabled)` | Manuelle Jogging-Achse X/Z setzen (beide Richtungsflags symmetrisch) |
| `pulse_gripper(dauer)` | Greifer-Impuls (`g_xPulseGripper`, wird garantiert wieder frei) |
| `release_brake()` | Motorbremse freigeben (`g_xMotorBrakeRelease`) |
| `reset_error()` | Fehler-Reset anfordern (`g_xErrorReset`) |
| `wait_done(t)` / `wait_home_ready(t)` / `wait_no_fault(t)` | Polling bis Bedingung oder Timeout (gibt `False`) |
| `wait_done_strict(t)` | wie `wait_done`, wirft `WaitTimeout` |

Die Bewegungswerte sind OPC-UA-Doubles (`i=11`); Skalierung und Einheiten sind nicht bestätigt.
Status- und Steuersignale werden als Booleans bzw. Integer geliefert –
Skalierungsfaktoren (z. B. 0,1 mm pro LSB) müssen aus der Anlagendoku kommen.

## 5. Sicherheit

- **Read-only per Default.** Gegen die reale Anlage wird jedes Write ohne
  `write_enabled=True` (bzw. `OPCUA_ALLOW_WRITES=1`) mit `PlcWriteDisabled`
  abgewiesen, bevor etwas die Leitung erreicht. Im Dry-Run ist das Fake die
  Sandbox – dort sind Writes immer erlaubt.
- **Keine Secrets im Code.** Endpunkt, User, Passwort, Freigaben kommen aus
  Umgebungsvariablen (`OPCUA_ENDPOINT`, `OPCUA_USERNAME`, `OPCUA_PASSWORD`,
  `OPCUA_TIMEOUT`, `OPCUA_POLL_INTERVAL`, `OPCUA_ALLOW_WRITES`).
- **Keine Hintergrundaktivität.** Keine Watchdog-/Auto-Reconnect-Aufgaben,
  keine Subscriptions; ein Request, eine Antwort.
- **Einfacher Reconnect.** Bei Transportverlust wird genau einmal neu
  verbunden; danach propagiert der Fehler zur Aufrufer-App.
- **Dry-Run.** `--dry-run` / `PlcConfig(dry_run=True)` ersetzt die Anlage
  durch eine deterministische In-Process-Simulation – ideal für
  Entwicklung und Tests ohne Anlage.

## 6. Wichtige Einschränkungen (aus der Scandoku)

1. **Semantik nicht verifiziert.** Die Zuordnung „wer schreibt was“
   (Roboter → SPS vs. SPS → Roboter) ist Namenskongruenz, kein belegtes
   Protokoll. Edge- vs. Level-Signale, Quittier-Sequenzen und Zykluszeiten
   sind unbekannt – vor Produktiveinsatz gegen das SPS-Programm prüfen.
2. **Typ `ns=3;i=3002`** der `g_w*`-Worte ist unbekannt (Namespace 3 wurde
   nicht gescannt); die Wörter werden hier als Ganzzahlen behandelt.
3. **Profilwahl:** Wie die SPS das aktive Profil aus `g_ascProfilePar`
   auswählt, ist nicht dokumentiert; `start_path(profil_index)` validiert
   den Index, schreibt ihn aber an keine Variable.
4. **Sicherheit:** Der Bootstrap spricht SecurityPolicy None (anonym, wie
   die Anlage aktuell konfiguriert ist). Username-Authentifizierung ist
   durchgekabelt, benötigt aber einen SignAndEncrypt-Kanal mit
   Zertifikaten – noch nicht implementiert.
5. **Tippfehler in der Anlage:** `g_xMotorBrakseReleased` (ohne `e`) wird
   exakt so adressiert.

## 7. Verwandte Module

- [`../opcua-doc/`](../opcua-doc/) – Metadatenscanner + Interfacespezifikation
  (Quelle aller NodeIds)
- [`../web-app/`](../web-app/) – (geplant) Flask-App, ruft diesen Client auf
