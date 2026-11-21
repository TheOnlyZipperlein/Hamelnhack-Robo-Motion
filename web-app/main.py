#!/usr/bin/env python3
"""Roboter-Chat Web-App (Flask, mobile first).

Stack:
    Browser (Chat + Spracheingabe)
      -> Flask (diese Datei)
          -> pi (RPC-Subprozess, JSONL)  ... LLM-Agent mit Roboter-Tools
          -> vLLM STT                     ... Audio -> Text
          -> sps_client (SyncPlc)         ... OPC UA -> SPS (GVL_Exchange)

Environment variables:
    WEB_HOST            bind address (default 0.0.0.0, LAN-accessible)
    WEB_PORT            port (default 8080)
    CHAT_TIMEOUT        seconds to wait for one agent run (default 180)
    STT_BASE            OpenAI-compatible base URL (default http://10.30.35.110:4001/v1)
    STT_MODEL           STT model id (default qwen)
    STT_LANGUAGE        forced language (default de)
    PI_LLM_BASE         LLM base URL for the pi provider (default = STT_BASE)
    PI_LLM_MODEL        LLM model id (default qwen)
    PI_WEB_APP_URL      URL the pi extension calls back to (default http://127.0.0.1:8080)
    OPCUA_*             sps_client settings (endpoint, dry-run, write gate, ...)

Run (repository root):
    set OPCUA_DRY_RUN=1 & set OPCUA_ALLOW_WRITES=1
    .\\.venv\\Scripts\\python.exe web-app\\main.py
"""

from __future__ import annotations

import json
import logging
import os
import queue
import shutil
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
import uuid
from dataclasses import asdict, is_dataclass
from pathlib import Path

from flask import Flask, jsonify, render_template, request

# Make the sibling sps-client package importable.
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "sps-client"))

from sps_client import PlcConfig, PlcConnectionError, SyncPlc  # noqa: E402

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger("webapp")

AGENT_DIR = Path(__file__).resolve().parent / "agent"

STT_BASE = os.environ.get("STT_BASE", "http://10.30.35.110:4001/v1")
STT_MODEL = os.environ.get("STT_MODEL", "stt")
STT_LANGUAGE = os.environ.get("STT_LANGUAGE", "de")
PI_LLM_BASE = os.environ.get("PI_LLM_BASE", STT_BASE)
PI_LLM_MODEL = os.environ.get("PI_LLM_MODEL", "qwen")
PI_WEB_APP_URL = os.environ.get("PI_WEB_APP_URL", "http://127.0.0.1:8080")
CHAT_TIMEOUT = float(os.environ.get("CHAT_TIMEOUT", "180"))


class PiAgentError(RuntimeError):
    """The pi agent process failed or refused a prompt."""


# ---------------------------------------------------------------------------
# pi RPC bridge (JSONL over stdin/stdout)
# ---------------------------------------------------------------------------
class PiBridge:
    """Long-lived `pi --mode rpc` subprocess with a JSONL protocol.

    One prompt at a time (serialized by a lock).  Assistant text is
    collected from `message_update`/`text_delta` events; tool activity from
    `tool_execution_start`/`tool_execution_end`; completion from
    `agent_settled`.
    """

    def __init__(self, agent_dir: Path, env: dict[str, str]) -> None:
        self.agent_dir = agent_dir
        self.env = env
        self.proc: subprocess.Popen[bytes] | None = None
        self.records: queue.Queue[dict] = queue.Queue()
        self._lock = threading.Lock()
        self._counter = 0
        self._alive = False

    # -- process lifecycle ---------------------------------------------
    def _cli_command(self) -> list[str]:
        node = shutil.which("node")
        if not node:
            raise PiAgentError("node executable not found (pi requires Node.js >= 22.19)")
        pkg = self.agent_dir / "node_modules" / "@earendil-works" / "pi-coding-agent"
        pj = json.loads((pkg / "package.json").read_text(encoding="utf-8"))
        bin_entry = pj.get("bin")
        rel = next(iter(bin_entry.values())) if isinstance(bin_entry, dict) else bin_entry
        cli = pkg / rel
        if not cli.exists():
            raise PiAgentError(f"pi CLI not found at {cli} (run: npm install in web-app/agent)")
        ext = self.agent_dir / ".pi" / "extensions" / "robot.ts"
        return [
            node, str(cli),
            "--mode", "rpc",
            "--no-session",
            "--provider", "local-vllm",
            "--model", PI_LLM_MODEL,
            "-e", str(ext),
        ]

    def start(self) -> None:
        cmd = self._cli_command()
        log.info("starting pi agent: %s", " ".join(cmd))
        self.proc = subprocess.Popen(
            cmd,
            cwd=str(self.agent_dir),
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env={**os.environ, **self.env},
        )
        threading.Thread(target=self._read_stdout, name="pi-stdout", daemon=True).start()
        threading.Thread(target=self._drain_stderr, name="pi-stderr", daemon=True).start()
        self._alive = True

    def stop(self) -> None:
        if self.proc is None:
            return
        self._alive = False
        with __import__("contextlib").suppress(Exception):
            assert self.proc.stdin is not None
            self.proc.stdin.close()  # orderly shutdown per pi RPC docs
        try:
            self.proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            self.proc.kill()
        log.info("pi agent stopped")

    @property
    def alive(self) -> bool:
        return self._alive and self.proc is not None and self.proc.poll() is None

    # -- protocol ---------------------------------------------------------
    def _read_stdout(self) -> None:
        assert self.proc is not None and self.proc.stdout is not None
        while True:
            line = self.proc.stdout.readline()
            if not line:
                break
            line = line.rstrip(b"\r\n")
            if not line:
                continue
            try:
                self.records.put(json.loads(line.decode("utf-8")))
            except json.JSONDecodeError:
                log.debug("non-JSON stdout line from pi: %.200s", line)

    def _drain_stderr(self) -> None:
        assert self.proc is not None and self.proc.stderr is not None
        for raw in self.proc.stderr:
            log.info("[pi] %s", raw.decode("utf-8", "replace").rstrip())

    def _send(self, payload: dict) -> None:
        assert self.proc is not None and self.proc.stdin is not None
        self.proc.stdin.write(json.dumps(payload, ensure_ascii=False).encode("utf-8") + b"\n")
        self.proc.stdin.flush()

    def prompt(self, message: str, timeout: float = CHAT_TIMEOUT) -> dict:
        """Send one user message; block until the agent settles."""
        with self._lock:
            if not self.alive:
                raise PiAgentError("pi agent process is not running")
            self._counter += 1
            rid = f"req-{self._counter}"
            self._send({"id": rid, "type": "prompt", "message": message})

            text_parts: list[str] = []
            tools: list[dict] = []
            open_tools: dict[str, dict] = {}
            deadline = time.monotonic() + timeout

            while True:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    with __import__("contextlib").suppress(Exception):
                        self._send({"id": f"abort-{rid}", "type": "abort"})
                    raise PiAgentError(f"agent run exceeded {timeout}s")
                try:
                    rec = self.records.get(timeout=min(remaining, 5.0))
                except queue.Empty:
                    if not self.alive:
                        raise PiAgentError("pi agent process exited unexpectedly")
                    continue

                rtype = rec.get("type")
                if rtype == "response" and rec.get("id") == rid:
                    if not rec.get("success"):
                        raise PiAgentError(f"prompt rejected: {rec.get('error')}")
                    disposition = (rec.get("data") or {}).get("disposition")
                    if disposition == "handled":
                        return {"reply": "", "tool_calls": tools, "note": "handled"}
                elif rtype == "message_update":
                    ev = rec.get("assistantMessageEvent") or {}
                    if ev.get("type") == "text_delta":
                        text_parts.append(ev.get("delta") or "")
                elif rtype == "tool_execution_start":
                    tid = rec.get("toolCallId") or f"anon-{len(tools)}"
                    open_tools[tid] = {
                        "tool": rec.get("toolName"),
                        "args": rec.get("args"),
                        "ok": None,
                    }
                elif rtype == "tool_execution_end":
                    tid = rec.get("toolCallId") or ""
                    entry = open_tools.pop(tid, None)
                    if entry is None:
                        entry = {"tool": rec.get("toolName"), "args": None}
                    entry["ok"] = not rec.get("isError", False)
                    tools.append(entry)
                elif rtype == "agent_settled":
                    break

            return {"reply": "".join(text_parts).strip(), "tool_calls": tools}


# ---------------------------------------------------------------------------
# STT forwarding (same multipart pattern as the original voice web app)
# ---------------------------------------------------------------------------
def build_stt_endpoint(base: str) -> str:
    base = base.strip().rstrip("/")
    if not base.startswith(("http://", "https://")):
        base = "http://" + base
    if base.endswith("/audio/transcriptions"):
        return base
    if not base.endswith("/v1"):
        base += "/v1"
    return base + "/audio/transcriptions"


def ext_for(content_type: str) -> str:
    ct = content_type.lower()
    for needle, ext in (("webm", "webm"), ("ogg", "ogg"), ("mp4", "mp4"), ("m4a", "m4a"), ("wav", "wav")):
        if needle in ct:
            return ext
    return "webm"


def transcribe_audio(audio: bytes, content_type: str) -> dict:
    boundary = uuid.uuid4().hex
    parts: list[bytes] = []

    def field(name: str, value: str) -> None:
        parts.append(
            f'--{boundary}\r\nContent-Disposition: form-data; name="{name}"\r\n\r\n{value}\r\n'.encode()
        )

    field("model", STT_MODEL)
    field("response_format", "json")
    if STT_LANGUAGE:
        field("language", STT_LANGUAGE)
    parts.append(
        (
            f'--{boundary}\r\nContent-Disposition: form-data; name="file"; '
            f'filename="audio.{ext_for(content_type)}"\r\n'
            f"Content-Type: {content_type.split(';')[0]}\r\n\r\n"
        ).encode()
    )
    parts.append(audio)
    parts.append(f"\r\n--{boundary}--\r\n".encode())

    req = urllib.request.Request(
        build_stt_endpoint(STT_BASE),
        data=b"".join(parts),
        headers={"Content-Type": f"multipart/form-data; boundary={boundary}"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=120) as resp:
        raw = resp.read().decode("utf-8", "replace")
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        return {"text": raw}


# ---------------------------------------------------------------------------
# Flask app
# ---------------------------------------------------------------------------
app = Flask(__name__, template_folder="templates")

plc: SyncPlc | None = None
plc_ok = False
bridge: PiBridge | None = None


def _plc_or_none() -> SyncPlc:
    if plc is None:
        raise PlcConnectionError("PLC client not initialized")
    return plc


@app.get("/")
def index() -> str:
    return render_template("index.html")


@app.get("/api/health")
def health() -> jsonify:
    return jsonify(
        {
            "plc": plc_ok,
            "pi": bool(bridge and bridge.alive),
            "plc_mode": "dry-run" if (plc and plc.config.dry_run) else "live",
        }
    )


@app.post("/api/chat")
def chat() -> jsonify:
    data = request.get_json(force=True, silent=True) or {}
    message = (data.get("message") or "").strip()
    if not message:
        return jsonify({"error": "empty message"}), 400
    if bridge is None or not bridge.alive:
        return jsonify({"error": "AI agent (pi) is not running"}), 503
    try:
        result = bridge.prompt(message)
    except PiAgentError as exc:
        log.warning("chat failed: %s", exc)
        return jsonify({"error": str(exc)}), 502
    return jsonify(result)


@app.post("/api/transcribe")
def transcribe() -> jsonify:
    audio = request.get_data(cache=True)
    if not audio:
        return jsonify({"error": "no audio received"}), 400
    try:
        result = transcribe_audio(audio, request.headers.get("Content-Type", "audio/webm"))
    except (urllib.error.URLError, OSError) as exc:
        log.warning("STT failed: %s", exc)
        return jsonify({"error": f"STT server unreachable: {exc}"}), 502
    return jsonify(result)


# ---- robot REST API (called by the pi extension AND available to clients) ----
def _robot_handler(fn):
    def wrapper(*args, **kwargs):
        global plc_ok
        if plc is None:
            return jsonify({"error": "PLC client not initialized"}), 503
        if not plc_ok:
            return jsonify({"error": "PLC not reachable"}), 503
        try:
            result = fn(*args, **kwargs)
            if is_dataclass(result):
                result = asdict(result)
            return jsonify(result)
        except PlcConnectionError as exc:
            plc_ok = False
            log.warning("PLC unreachable: %s", exc)
            return jsonify({"error": f"PLC nicht erreichbar: {exc}"}), 503
        except (ValueError, KeyError) as exc:
            return jsonify({"error": str(exc)}), 400
        except Exception as exc:  # noqa: BLE001 - report, don't crash
            log.exception("robot API failure")
            return jsonify({"error": str(exc)}), 500

    # Unique Flask endpoint per route (closures all share the name "wrapper").
    wrapper.__name__ = fn.__name__
    return wrapper


@app.get("/api/robot/status")
@_robot_handler
def robot_status():
    return _plc_or_none().status()


@app.get("/api/robot/positions")
@_robot_handler
def robot_positions():
    return _plc_or_none().positions()


@app.get("/api/robot/profiles/<int:index>")
@_robot_handler
def robot_get_profile(index: int):
    return _plc_or_none().get_profile(index)


@app.post("/api/robot/profiles/<int:index>")
@_robot_handler
def robot_set_profile(index: int):
    fields = request.get_json(force=True, silent=True) or {}
    allowed = {
        "x_pos", "z_pos", "path_vel", "path_acc",
        "path_dec", "blending_radius", "x_halt",
    }
    _plc_or_none().set_profile(index, **{k: v for k, v in fields.items() if k in allowed})
    return {"ok": True, "index": index}


@app.post("/api/robot/path/start")
@_robot_handler
def robot_path_start():
    body = request.get_json(force=True, silent=True) or {}
    kwargs = {k: body[k] for k in ("profile_index", "path_vel_out", "override") if k in body}
    _plc_or_none().start_path(**kwargs)
    return {"ok": True}


for _path, _fn in (
    ("/api/robot/path/halt", "halt_path"),
    ("/api/robot/path/hold", "hold_path"),
    ("/api/robot/path/stop", "stop_path"),
    ("/api/robot/path/interrupt", "interrupt_path"),
):
    def _make(_fn_name: str):
        def _route():
            getattr(_plc_or_none(), _fn_name)()
            return {"ok": True}
        _route.__name__ = _fn_name
        app.post(_path)(_robot_handler(_route))
    _make(_fn)


@app.post("/api/robot/home")
@_robot_handler
def robot_home():
    _plc_or_none().home()
    return {"ok": True}


@app.post("/api/robot/jog")
@_robot_handler
def robot_jog():
    body = request.get_json(force=True, silent=True) or {}
    enabled = body.get("enabled")
    _plc_or_none().jog(
        body.get("axis", ""),
        bool(body.get("positive", True)),
        True if enabled is None else bool(enabled),
    )
    return {"ok": True}


@app.post("/api/robot/gripper")
@_robot_handler
def robot_gripper():
    body = request.get_json(force=True, silent=True) or {}
    _plc_or_none().pulse_gripper(float(body.get("duration", 0.5)))
    return {"ok": True}


@app.post("/api/robot/brake/release")
@_robot_handler
def robot_brake_release():
    _plc_or_none().release_brake()
    return {"ok": True}


@app.post("/api/robot/error/reset")
@_robot_handler
def robot_error_reset():
    _plc_or_none().reset_error()
    return {"ok": True}


_CONDITIONS = {
    "done": "wait_done",
    "home_ready": "wait_home_ready",
    "no_fault": "wait_no_fault",
}


@app.post("/api/robot/wait")
@_robot_handler
def robot_wait():
    body = request.get_json(force=True, silent=True) or {}
    cond = body.get("condition", "done")
    if cond not in _CONDITIONS:
        raise ValueError(f"condition must be one of {sorted(_CONDITIONS)}")
    timeout = float(body.get("timeout", 30))
    ok = getattr(_plc_or_none(), _CONDITIONS[cond])(timeout)
    return {"ok": ok, "condition": cond}


# ---------------------------------------------------------------------------
# startup / shutdown
# ---------------------------------------------------------------------------
def main() -> int:
    global plc, plc_ok, bridge

    host = os.environ.get("WEB_HOST", "0.0.0.0")
    port = int(os.environ.get("WEB_PORT", "8080"))

    config = PlcConfig.from_env()
    log.info("PLC config: %s", config.describe())
    plc = SyncPlc(config, connect=False)
    try:
        plc.connect(timeout=15)
        plc_ok = True
        log.info("PLC session established")
    except Exception as exc:  # noqa: BLE001 - app must survive without the plant
        plc_ok = False
        log.warning("PLC not reachable at startup: %s (robot API will return 503)", exc)

    bridge = PiBridge(
        AGENT_DIR,
        env={
            "PI_WEB_APP_URL": PI_WEB_APP_URL,
            "PI_LLM_BASE": PI_LLM_BASE,
            "PI_LLM_MODEL": PI_LLM_MODEL,
        },
    )
    try:
        bridge.start()
        log.info("pi agent started")
    except PiAgentError as exc:
        log.error("pi agent unavailable: %s (chat will return 503)", exc)
        bridge = None

    log.info("web app listening on http://%s:%d", "localhost" if host == "127.0.0.1" else host, port)
    try:
        app.run(host=host, port=port, threaded=True)
    finally:
        if bridge is not None:
            bridge.stop()
        if plc is not None:
            plc.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
