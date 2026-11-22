# Hamelnhack Robo-Motion

Sprach- und Chatsteuerung für die c500-Zeichen-/Schweißanlage (X/Z-Portal)
über OPC UA. Ein KI-Agent ([pi](https://github.com/earendil-works/pi)) plant
Bahnen, prüft sie im Simulator und gegen ein hochgeladenes Werkstückbild
und fährt sie anschließend auf der realen Anlage ab.

```
Handy/Browser (Chat, 🎙, Canvas)
  ↕ HTTPS (LAN)
web-app  (Flask + pi-Agent)  ──  LiteLLM (LLM + STT)
  ↕
sps-client  (Robot | SimRobot, Routenplaner, Dry-Run)
  ↕ OPC UA
SPS c500  (GVL_Exchange)
```

## Ordner

| Ordner | Inhalt |
| --- | --- |
| [`web-app/`](web-app/) | Mobile-first Chat-App, pi-Agent mit Roboter-Tools, Canvas, Spracheingabe, REST-API |
| [`sps-client/`](sps-client/) | Python-Library: OPC-UA-Client, `Robot` (live), `SimRobot` (Simulator), Routenplanung/-prüfung, Tests |
| [`opcua-doc/`](opcua-doc/) | Read-only-Scanner und **verbindliche Schnittstellenspezifikation** [`OPCUA_ROBOT_INTERFACE.md`](opcua-doc/OPCUA_ROBOT_INTERFACE.md) |
| [`docs/`](docs/) | Konzeptnotizen (z. B. Routenverifikation) |
| `Emulator/` | Ursprünglicher Emulator-Prototyp |
| `schweiß-test.png` | Testbild mit Schweißnaht für die Bildverfolgung |

## Schnellstart

```powershell
# Einmalig
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r web-app\requirements.txt -r sps-client\requirements.txt
cd web-app\agent; npm install --ignore-scripts; cd ..\..
winget install Gyan.FFmpeg          # für die Spracheingabe

# Simulator (ohne Anlage, ungefährlich)
$env:ROBOT_BACKEND='sim'; $env:WEB_PORT='8091'
Remove-Item Env:PI_WEB_APP_URL -ErrorAction SilentlyContinue
.\.venv\Scripts\python.exe -X utf8 web-app\main.py

# Live-Anlage (nur im Netz 192.168.5.x erreichbar)
$env:ROBOT_BACKEND='live'; $env:OPCUA_ALLOW_WRITES='1'; $env:CHAT_TIMEOUT='900'
.\.venv\Scripts\python.exe -X utf8 web-app\main.py
```

Danach `http(s)://<PC-IP>:<Port>` im Browser öffnen. HTTPS (nötig fürs
Mikrofon am Handy) und alle Variablen: siehe [`web-app/README.md`](web-app/README.md).

## Funktionsweise in Kürze

- **Zeichnen = Jog.** Live fährt der Server die Wegpunkte achsparallel per
  Jog ab (Diagonalen/Bögen als Treppe). `g_xPathExecute` wird live nicht
  genutzt. Simulator und Dry-Run fahren dieselben Legs.
- **Agent-Loop:** `plan_route` → `check_route_on_image` ⇄ `update_route`
  → `simulate_route` (Dry-Run, berührt die SPS nicht) → `set_path_waypoints`
  → `start_path_run` → `compare_run`. Feste Grenzen: Dry-Run-Abweichung
  ≤ 1,5, Abstand zum Bildmerkmal ≤ 2 Einheiten.
- **Sichere Box:** `x ∈ [30, 260]`, `z ∈ [35, 150]`. Zeichenstart (30, 35),
  Home (15, −10) liegt außerhalb und ist nur per Homing erreichbar.
- **Reset** (Header-Button / `POST /api/reset`): neue Agent-Sitzung,
  Befehlsflags gelöscht, Fahrt zum Zeichenstart.

## Sicherheit

- Schreibzugriff auf die SPS nur mit `OPCUA_ALLOW_WRITES=1`.
- Die App hat **keine Anmeldung** – nur im LAN betreiben.
- Vor Arbeiten an der realen Anlage die Live-Erkenntnisse und den
  Sicherheitsworkflow in [`AGENTS.md`](AGENTS.md) lesen (Grenzen, Fehler
  21005, ignorierte Warnung 20434, Homing-Rezept).
- Keine Zertifikatsschlüssel oder Tokens ins Repo; TLS-Dateien liegen unter
  `%LOCALAPPDATA%\RoboMotion-TLS`.
