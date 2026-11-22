# Roboter-Chat Web-App (`web-app`)

Mobile-first Chat-App (Flask), über die ein Mensch – oder jeder andere
Client – den Roboter der c500-Anlage per Sprache oder Text steuert.
Der „Kopf“ ist **[pi](https://github.com/earendil-works/pi)** (AI-Agent-Harness),
der als RPC-Subprozess läuft und Roboter-Tools bekommt; die Hände ist der
[`sps_client`](../sps-client/) (OPC UA → SPS).

```
Handy/Browser (Chat + 🎙)
  ↕ HTTP/HTTPS (LAN)
Flask  web-app/main.py
  ├── /api/chat        → pi --mode rpc (JSONL, Resident-Subprozess)
  │                        └── Extension agent/.pi/extensions/robot.ts
  │                            ├── registerProvider → LiteLLM (10.30.35.110:4001)
  │                            └── Roboter-/Routen-Tools → HTTP → /api/*
  ├── /api/transcribe  → FFmpeg (WAV 16 kHz) → LiteLLM /v1/audio/transcriptions
  ├── /api/route/*     → Planer, Dry-Run, Bildprüfung (sps_client.route)
  └── /api/robot/*     → Robot (live, OPC UA) | SimRobot (ROBOT_BACKEND=sim)
```

## 1. Voraussetzungen

- Python ≥ 3.11, `flask` (siehe `requirements.txt`)
- Node.js ≥ 22.19 (für pi)
- `sps-client` im Nachbarordner (liegt im Repo)
- LLM + STT Server (OpenAI-kompatibel, LiteLLM) erreichbar
- FFmpeg (`winget install Gyan.FFmpeg`) für die Spracheingabe
- Für Live-Betrieb: OPC-UA-Endpunkt der SPS erreichbar

## 2. Einrichtung (einmalig)

```powershell
# 1) Python-Abhängigkeiten:
.\.venv\Scripts\python.exe -m pip install -r web-app\requirements.txt
#    (oder: uv pip install --python .venv\Scripts\python.exe -r web-app\requirements.txt)

# 2) pi-Runtime (Node) installieren:
cd web-app\agent
npm install --ignore-scripts
cd ..\..
```

## 3. Start

Aus dem Projektordner (PowerShell):

```powershell
# Simulator OHNE Anlage (Canvas-Physik nach Live-Messwerten):
$env:ROBOT_BACKEND="sim"; $env:WEB_PORT="8091"
Remove-Item Env:PI_WEB_APP_URL -ErrorAction SilentlyContinue
.\.venv\Scripts\python.exe -X utf8 web-app\main.py

# Live-Anlage (Schreibzugriff für die Agenten-Tools):
$env:ROBOT_BACKEND="live"; $env:OPCUA_ALLOW_WRITES="1"; $env:CHAT_TIMEOUT="900"
.\.venv\Scripts\python.exe -X utf8 web-app\main.py
```

Danach im Handy-Browser öffnen: `http://<PC-IP>:8080` (mit TLS `https://…`).
Beim Start fährt die Live-Anlage zum Zeichenstart (30, 35), außer
`STARTUP_DRAWING_START=0`. Nie den Server beenden, während die Achse fährt.

## 4. Konfiguration (Umgebungsvariablen)

| Variable | Default | Bedeutung |
| --- | --- | --- |
| `ROBOT_BACKEND` | `live` | `live` = Anlage über OPC UA, `sim` = In-Process-Simulator |
| `STARTUP_DRAWING_START` | `1` | `0` = beim Start nicht zum Zeichenstart fahren |
| `WEB_HOST` | `0.0.0.0` | Bind-Adresse (LAN-Zugang) |
| `WEB_PORT` | `8080` | Port |
| `WEB_TLS_CERT`, `WEB_TLS_KEY` | – | Serverzertifikat und privater Schlüssel (PEM); schalten HTTPS ein |
| `WEB_TLS_CA` | – | Lokale CA (PEM) für die Zertifikatsprüfung durch den pi-Prozess |
| `CHAT_TIMEOUT` | `180` | Sekunden bis ein Agent-Lauf als Timeout gilt (live `900` empfohlen) |
| `STT_BASE` | `http://10.30.35.110:4001/v1` | STT-Server (OpenAI-komp.) |
| `STT_MODEL` | `stt` | STT-Modell-ID (dedizierte Gruppe; `qwen` unterstützt kein Audio) |
| `STT_LANGUAGE` | `de` | erzwungene Sprache |
| `STT_FFMPEG` | `ffmpeg` aus `PATH` | Optionaler Pfad zu FFmpeg für die Audio-Konvertierung |
| `PI_LLM_BASE` | = `STT_BASE` | LLM-Server für pi |
| `PI_LLM_MODEL` | `terra` | LLM-Modell-ID (getestet: `terra`, `sol`, `luna`, `qwen-*`) |
| `PI_THINKING` | `xhigh` | Reasoning-Stufe für pi |
| `PI_WEB_APP_URL` | Loopback auf `WEB_PORT` (HTTP bzw. HTTPS) | Callback-URL für die pi-Extension; normalerweise nicht setzen |
| `OPCUA_ENDPOINT` | `opc.tcp://192.168.5.99:4840` | SPS-Endpunkt |
| `OPCUA_DRY_RUN` | – | `1` = Fake-PLC (Entwicklung) |
| `OPCUA_ALLOW_WRITES` | – | `1` = Schreibzugriff auf die SPS (sonst read-only) |
| `OPCUA_TIMEOUT`, `OPCUA_POLL_INTERVAL` | `5`, `0.1` | sps_client-Defaults |

## 5. API

| Route | Methode | Zweck |
| --- | --- | --- |
| `/` | GET | Chat-UI (mobile first) |
| `/api/health` | GET | `{plc, pi, plc_mode}` für Status-Badges |
| `/api/chat` | POST `{message}` | `{reply, tool_calls[]}` – ein kompletter Agent-Lauf |
| `/api/chat/activity` | GET | Live-Aktivität des Agenten (laufende Tools) |
| `/api/reset` | POST | Neue Agent-Sitzung, Flags löschen, Fahrt zum Zeichenstart |
| `/api/transcribe` | POST (audio/MIME) | `{text}` – FFmpeg-Normalisierung + STT (max. 8 MiB / 120 s) |
| `/api/robot/status` | GET | Status-Snapshot |
| `/api/robot/positions` | GET | Soll/Ist-Werte |
| `/api/robot/profiles/<i>` | GET/POST | Profil lesen / teilweisen schreiben |
| `/api/robot/path/start` | POST | Bank (Slot 1 bis `x_halt`) per Jog zeichnen (live) bzw. simulieren |
| `/api/robot/path/halt\|hold\|stop\|interrupt` | POST | Stoppvarianten; `stop` bricht auch Jog-Zeichnen/Positionierung ab |
| `/api/robot/home`, `/api/robot/home/clear` | POST | Homing starten / Home-Flag löschen |
| `/api/robot/drawing-start` | POST | Homing + Jog nach (30, 35) |
| `/api/robot/tracking` | GET | Position, Geschwindigkeit, Fehler für das Canvas |
| `/api/robot/jog` | POST | `{axis: x\|z, positive, enabled?}` |
| `/api/robot/gripper` | POST | `{duration?}` |
| `/api/robot/brake/release` | POST | Bremse frei |
| `/api/robot/error/reset` | POST | Fehler-Reset |
| `/api/robot/wait` | POST | `{condition: done\|home_ready\|no_fault, timeout?}` |
| `/api/route/plan` | POST | Segment-Spec → ≤ 10 Wegpunkte |
| `/api/route/simulate` | POST | Headless Dry-Run (berührt die SPS nicht) + Vorschau-PNG |
| `/api/route/check-image` | POST | Übersicht + Zoom-Kacheln der Route über dem Hintergrundbild |
| `/api/route/preview/<id>.png`, `/api/route/last`, `/api/route/history`, `/api/route/actual` | GET | Vorschauen, letzte/alle Dry-Runs, Ist-Samples |
| `/api/drawing`, `/api/drawing/parts/<id>/delete`, `/api/drawing/clear` | GET/POST | Gezeichnete Teile (Stift-Modell) |
| `/api/background`, `/api/background/info`, `/api/background/<kind>` | POST/GET/DELETE | Hintergrundbild (nur im Speicher) |
| `/api/sim/state`, `/api/sim/reset`, `/api/sim/speed` | GET/POST | Nur Simulator |

Die `/api/robot/*`-Routes sind sowohl für die pi-Extension als auch für
externe Clients gedacht – die Web-App ist damit auch ohne Chat nutzbare
REST-Schnittstelle.

## 6. Sicherheit & Grenzen

- **LAN-offen:** `WEB_HOST=0.0.0.0` macht die App für alle Geräte im
  Netzwerk erreichbar; es gibt bewusst **keine** Authentifizierung
  (Hackathon-Prototyp). Nicht ins Internet exponieren.
- **Schreibzugriff:** Ohne `OPCUA_ALLOW_WRITES=1` verweigert der
  `sps_client` jeden SPS-Write (Gate in `sps-client/sps_client/plc.py`);
  die Agenten-Tools scheitern dann sauber mit HTTP 503/4xx.
- **Semantik nicht verifiziert:** Rollen der GVL-Variablen sind
  Namenskongruenz (siehe `opcua-doc/OPCUA_ROBOT_INTERFACE.md`, Abschnitt 7).
  Der Agent erhält per `agent/AGENTS.md` strenge Operatorenregeln
  (erst lesen, dann handeln; bei Fehler anhalten; keine Einheiten erfinden).
- **Mikrofon:** Browser verlangen für `getUserMedia` einen sicheren Kontext.
  Am Handy die lokale CA als vertrauenswürdig installieren und die App per
  `https://<PC-IP>:8080` öffnen. HTTPS ersetzt keine Anmeldung.
- **Reasoning-Modelle** verbrauchen viele Tokens; Jog-Zeichnen ist langsam
  (Kreis r=40 > 120 s). `CHAT_TIMEOUT` live auf `900` setzen.
- **Neben-Server:** `PI_WEB_APP_URL` nicht setzen – sonst kann ein
  Sim-Chat die reale Anlage über den Live-Server steuern.
- **Neustart** verliert Hintergrundbild, Dry-Run-Historie, Zeichnung und
  Chat-Sitzung (alles nur im Speicher).

### HTTPS mit eigener CA (PowerShell)

Vor dem Start setzen (zusätzlich zur üblichen Backend-Konfiguration):

```powershell
$tls = Join-Path $env:LOCALAPPDATA 'RoboMotion-TLS'
$env:WEB_TLS_CERT = Join-Path $tls 'server.pem'
$env:WEB_TLS_KEY = Join-Path $tls 'server.key'
$env:WEB_TLS_CA = Join-Path $tls 'ca.pem'
Remove-Item Env:PI_WEB_APP_URL -ErrorAction SilentlyContinue
```

Das Serverzertifikat muss die LAN-IP und die Callback-IP `127.0.0.1`
als IP-SANs enthalten. Nur `RoboMotion-CA.cer` ans Handy übertragen,
niemals die privaten Schlüssel. Auf iOS nach der Installation zusätzlich
unter **Einstellungen → Allgemein → Info → Zertifikatsvertrauenseinstellungen**
das volle Vertrauen aktivieren. Auf Android als **CA-Zertifikat** installieren.
Die CA nach der Demo vom Handy entfernen, wenn sie nicht mehr gebraucht wird.

## 7. Aufbau

| Datei | Rolle |
| --- | --- |
| `main.py` | Flask-App: pi-RPC-Brücke, STT mit FFmpeg, Robot-/Route-REST, HTTPS |
| `templates/index.html` | Mobile-first Chat-UI + Canvas, Jogpad, Aktivitätsanzeige (Vanilla JS) |
| `agent/package.json` | pi-Dependency (pinned) |
| `agent/.pi/extensions/robot.ts` | pi-Extension: LLM-Provider + Roboter-Tools |
| `agent/AGENTS.md` | System-Kontext/Operatorenregeln für den Agenten |
