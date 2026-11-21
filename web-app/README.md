# Roboter-Chat Web-App (`web-app`)

Mobile-first Chat-App (Flask), über die ein Mensch – oder jeder andere
Client – den Roboter der c500-Anlage per Sprache oder Text steuert.
Der „Kopf“ ist **[pi](https://github.com/earendil-works/pi)** (AI-Agent-Harness),
der als RPC-Subprozess läuft und Roboter-Tools bekommt; die Hände ist der
[`sps_client`](../sps-client/) (OPC UA → SPS).

```
Handy/Browser (Chat + 🎙)
  ↕ HTTP (LAN)
Flask  web-app/main.py
  ├── /api/chat        → pi --mode rpc (JSONL, Resident-Subprozess)
  │                        └── Extension agent/.pi/extensions/robot.ts
  │                            ├── registerProvider → lokaler vLLM (10.30.35.110:4001)
  │                            └── 12 Roboter-Tools → HTTP → /api/robot/*
  ├── /api/transcribe  → vLLM /v1/audio/transcriptions
  └── /api/robot/*     → sps_client (SyncPlc) → OPC UA → GVL_Exchange
```

## 1. Voraussetzungen

- Python ≥ 3.11, `flask` (siehe `requirements.txt`)
- Node.js ≥ 22.19 (für pi)
- `sps-client` im Nachbarordner (liegt im Repo)
- LLM + STT Server (OpenAI-kompatibel, vLLM) erreichbar
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
# Entwicklung OHNE Anlage (Fake-PLC, Schreiben erlaubt):
$env:OPCUA_DRY_RUN="1"; $env:OPCUA_ALLOW_WRITES="1"
.\.venv\Scripts\python.exe web-app\main.py

# Live-Anlage (Schreibzugriff für die Agenten-Tools):
$env:OPCUA_ALLOW_WRITES="1"
.\.venv\Scripts\python.exe web-app\main.py
```

Danach im Handy-Browser öffnen: `http://<PC-IP>:8080`

## 4. Konfiguration (Umgebungsvariablen)

| Variable | Default | Bedeutung |
| --- | --- | --- |
| `WEB_HOST` | `0.0.0.0` | Bind-Adresse (LAN-Zugang) |
| `WEB_PORT` | `8080` | Port |
| `CHAT_TIMEOUT` | `180` | Sekunden bis ein Agent-Lauf als Timeout gilt |
| `STT_BASE` | `http://10.30.35.110:4001/v1` | STT-Server (OpenAI-komp.) |
| `STT_MODEL` | `stt` | STT-Modell-ID (dedizierte Gruppe; `qwen` unterstützt kein Audio) |
| `STT_LANGUAGE` | `de` | erzwungene Sprache |
| `PI_LLM_BASE` | = `STT_BASE` | LLM-Server für pi |
| `PI_LLM_MODEL` | `qwen` | LLM-Modell-ID |
| `PI_WEB_APP_URL` | `http://127.0.0.1:8080` | Callback-URL für die pi-Extension |
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
| `/api/transcribe` | POST (audio/MIME) | `{text}` – STT-Forward |
| `/api/robot/status` | GET | Status-Snapshot |
| `/api/robot/positions` | GET | Soll/Ist-Werte |
| `/api/robot/profiles/<i>` | GET/POST | Profil lesen / teilweisen schreiben |
| `/api/robot/path/start` | POST | `{profile_index?, path_vel_out?, override?}` |
| `/api/robot/path/halt\|hold\|stop\|interrupt` | POST | Bahnlauf-Stoppvarianten |
| `/api/robot/home` | POST | Homing |
| `/api/robot/jog` | POST | `{axis: x\|z, positive, enabled?}` |
| `/api/robot/gripper` | POST | `{duration?}` |
| `/api/robot/brake/release` | POST | Bremse frei |
| `/api/robot/error/reset` | POST | Fehler-Reset |
| `/api/robot/wait` | POST | `{condition: done\|home_ready\|no_fault, timeout?}` |

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
- **Mikrofon:** Browser verlangen für `getUserMedia` einen sicheren Kontext
  (HTTPS oder localhost). Vom Handy per `http://<IP>:8080` funktioniert
  die Texteingabe immer; das Mikrofon braucht zusätzlich HTTPS
  (Reverse Proxy) oder den Browser-Flag
  `unsafely-treat-insecure-origin-as-secure`.
- **Reasoning-Modell:** Der lokale `qwen`-Modell verbraucht viele Tokens
  für internes Reasoning; `CHAT_TIMEOUT` großzügig halten.

## 7. Aufbau

| Datei | Rolle |
| --- | --- |
| `main.py` | Flask-App: pi-RPC-Brücke, STT-Forward, Robot-REST |
| `templates/index.html` | Mobile-first Chat-UI (Vanilla JS, kein Build-Schritt) |
| `agent/package.json` | pi-Dependency (pinned) |
| `agent/.pi/extensions/robot.ts` | pi-Extension: LLM-Provider + Roboter-Tools |
| `agent/AGENTS.md` | System-Kontext/Operatorenregeln für den Agenten |
