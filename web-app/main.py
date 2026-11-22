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
    WEB_TLS_CERT        PEM server certificate (optional HTTPS)
    WEB_TLS_KEY         PEM server private key (required with WEB_TLS_CERT)
    WEB_TLS_CA          PEM local CA trusted by the pi subprocess (optional)
    CHAT_TIMEOUT        seconds to wait for one agent run (default 180)
    STT_BASE            OpenAI-compatible base URL (default http://10.30.35.110:4001/v1)
    STT_MODEL           STT model id (default stt)
    STT_FFMPEG          FFmpeg executable (default ffmpeg from PATH)
    STT_LANGUAGE        forced language (default de)
    PI_LLM_BASE         LLM base URL for the pi provider (default = STT_BASE)
    PI_LLM_MODEL        LLM model id (default terra)
    PI_WEB_APP_URL      URL the pi extension calls back to (default loopback on WEB_PORT)
    OPCUA_*             sps_client settings (endpoint, dry-run, write gate, ...)
    ROBOT_BACKEND       "live" (default) or "sim" (in-process SimRobot, no plant)

Run (repository root):
    set OPCUA_DRY_RUN=1 & set OPCUA_ALLOW_WRITES=1
    .\\.venv\\Scripts\\python.exe web-app\\main.py
"""

from __future__ import annotations

import io
import json
import logging
import math
import os
import queue
import shutil
import ssl
import subprocess
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.request
import uuid
import wave
from collections import OrderedDict
from dataclasses import asdict, is_dataclass
from pathlib import Path

from asyncua.ua.uaerrors import BadNodeIdUnknown
from flask import Flask, jsonify, render_template, request
from werkzeug.exceptions import RequestEntityTooLarge

# Make the sibling sps-client package importable.
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "sps-client"))

from sps_client import PlcConfig, PlcConnectionError, SimRobot, SyncPlc  # noqa: E402
from sps_client.route import (  # noqa: E402
    compare_run,
    plan_route,
    render_feature_check,
    render_route_view,
    verify_route,
)
from sps_client.simulator import SimResult, simulate_path  # noqa: E402

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger("webapp")

AGENT_DIR = Path(__file__).resolve().parent / "agent"

STT_BASE = os.environ.get("STT_BASE", "http://10.30.35.110:4001/v1")
STT_MODEL = os.environ.get("STT_MODEL", "stt")
STT_LANGUAGE = os.environ.get("STT_LANGUAGE", "de")
PI_LLM_BASE = os.environ.get("PI_LLM_BASE", STT_BASE)
PI_LLM_MODEL = os.environ.get("PI_LLM_MODEL", "terra")
# pi thinking level: off|minimal|low|medium|high|xhigh|max
PI_THINKING = os.environ.get("PI_THINKING", "xhigh")
CHAT_TIMEOUT = float(os.environ.get("CHAT_TIMEOUT", "180"))
ROBOT_BACKEND = os.environ.get("ROBOT_BACKEND", "live").strip().lower()
IS_SIM = ROBOT_BACKEND == "sim"


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
        self._wlock = threading.Lock()  # stdin writes (prompt + reset/abort)
        self._counter = 0
        self._alive = False
        # Live view of the current/last run for the chat UI (GET /api/chat/activity).
        self._act_lock = threading.Lock()
        self.generation = 0  # bumped on every reset; stale chat replies are dropped
        self.activity: dict = {"run": 0, "running": False, "items": []}

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
            "--thinking", PI_THINKING,
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
        # Readers are bound to THIS process and queue (a reset may restart pi).
        threading.Thread(target=self._read_stdout, args=(self.proc, self.records),
                         name="pi-stdout", daemon=True).start()
        threading.Thread(target=self._drain_stderr, args=(self.proc,),
                         name="pi-stderr", daemon=True).start()
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
    @staticmethod
    def _read_stdout(proc: subprocess.Popen, records: queue.Queue) -> None:
        assert proc.stdout is not None
        while True:
            line = proc.stdout.readline()
            if not line:
                break
            line = line.rstrip(b"\r\n")
            if not line:
                continue
            try:
                records.put(json.loads(line.decode("utf-8")))
            except json.JSONDecodeError:
                log.debug("non-JSON stdout line from pi: %.200s", line)

    @staticmethod
    def _drain_stderr(proc: subprocess.Popen) -> None:
        assert proc.stderr is not None
        for raw in proc.stderr:
            log.info("[pi] %s", raw.decode("utf-8", "replace").rstrip())

    def _send(self, payload: dict) -> None:
        assert self.proc is not None and self.proc.stdin is not None
        with self._wlock:
            self.proc.stdin.write(json.dumps(payload, ensure_ascii=False).encode("utf-8") + b"\n")
            self.proc.stdin.flush()

    # -- activity (what the agent is doing right now) -----------------------
    @staticmethod
    def _clip(value, limit: int) -> str:
        text = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False, default=str)
        return text if len(text) <= limit else text[: limit - 1] + "…"

    @staticmethod
    def _result_text(result) -> str:
        if isinstance(result, dict) and isinstance(result.get("content"), list):
            text = "".join(c.get("text", "") for c in result["content"] if isinstance(c, dict))
        else:
            text = "" if result is None else str(result)
        try:  # pretty-printed JSON from the tools -> one compact line
            return json.dumps(json.loads(text), ensure_ascii=False, separators=(", ", ": "))
        except (ValueError, TypeError):
            return text

    def _act_begin(self, message: str) -> None:
        with self._act_lock:
            self.activity = {"run": self.activity["run"] + 1, "running": True,
                             "message": message, "started": time.time(), "items": []}

    def _act_stream(self, kind: str, delta: str) -> None:
        """Append a thinking/text delta to the last block of that kind (or open one)."""
        with self._act_lock:
            items = self.activity["items"]
            if not items or items[-1]["type"] != kind:
                items.append({"type": kind, "text": ""})
            items[-1]["text"] = (items[-1]["text"] + delta)[-4000:]

    def _act_tool(self, tid: str, **fields) -> None:
        with self._act_lock:
            for item in reversed(self.activity["items"]):
                if item["type"] == "tool" and item["id"] == tid:
                    item.update(fields)
                    return
            self.activity["items"].append({"type": "tool", "id": tid, **fields})

    def _act_note(self, text: str) -> None:
        with self._act_lock:
            self.activity["items"].append({"type": "note", "text": text})

    def _act_end(self) -> None:
        with self._act_lock:
            self.activity["running"] = False
            for item in self.activity["items"]:
                if item["type"] == "tool" and item.get("status") == "running":
                    item["status"] = "aborted"

    def activity_snapshot(self) -> dict:
        with self._act_lock:
            return json.loads(json.dumps({**self.activity, "generation": self.generation}))

    def prompt(self, message: str, timeout: float = CHAT_TIMEOUT, *,
               images: list[dict] | None = None, display: str | None = None) -> dict:
        """Send one user message; block until the agent settles.

        ``images``: pi ImageContent dicts ({type, mimeType, data}) attached
        to this user message. ``display``: text for the activity panel
        (defaults to ``message``)."""
        with self._lock:
            if not self.alive:
                raise PiAgentError("pi agent process is not running")
            self._counter += 1
            rid = f"req-{self._counter}"
            generation = self.generation
            self._act_begin((display or message) + (" [+ Hintergrundbild]" if images else ""))
            try:
                result = self._run_prompt(rid, message, timeout, generation, images)
            finally:
                self._act_end()
            result["generation"] = generation
            if generation != self.generation:
                result["stale"] = True  # a reset happened meanwhile
            return result

    def _run_prompt(self, rid: str, message: str, timeout: float, generation: int,
                    images: list[dict] | None = None) -> dict:
        records = self.records
        cmd = {"id": rid, "type": "prompt", "message": message}
        if images:
            cmd["images"] = images
        self._send(cmd)

        text_parts: list[str] = []
        tools: list[dict] = []
        open_tools: dict[str, dict] = {}
        deadline = time.monotonic() + timeout

        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                with __import__("contextlib").suppress(Exception):
                    self._send({"id": f"abort-{rid}", "type": "abort"})
                self._act_note(f"Zeitlimit {timeout:.0f}s überschritten – abgebrochen")
                raise PiAgentError(f"agent run exceeded {timeout}s")
            if generation != self.generation and records is not self.records:
                raise PiAgentError("chat was reset")  # agent process was restarted
            try:
                rec = records.get(timeout=min(remaining, 0.5))
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
                etype = ev.get("type")
                if etype == "text_delta":
                    text_parts.append(ev.get("delta") or "")
                    self._act_stream("text", ev.get("delta") or "")
                elif etype == "thinking_delta":
                    self._act_stream("thinking", ev.get("delta") or "")
                elif etype == "error":
                    self._act_note(f"Modellfehler: {self._clip(ev.get('error') or ev.get('reason'), 300)}")
            elif rtype == "tool_execution_start":
                tid = rec.get("toolCallId") or f"anon-{len(tools)}"
                open_tools[tid] = {
                    "tool": rec.get("toolName"),
                    "args": rec.get("args"),
                    "ok": None,
                }
                self._act_tool(tid, name=rec.get("toolName"), status="running",
                               args=self._clip(rec.get("args") or {}, 400),
                               started=time.time())
            elif rtype == "tool_execution_update":
                tid = rec.get("toolCallId") or ""
                self._act_tool(tid, name=rec.get("toolName"), status="running",
                               result=self._clip(self._result_text(rec.get("partialResult")), 600))
            elif rtype == "tool_execution_end":
                tid = rec.get("toolCallId") or ""
                entry = open_tools.pop(tid, None)
                if entry is None:
                    entry = {"tool": rec.get("toolName"), "args": None}
                entry["ok"] = not rec.get("isError", False)
                tools.append(entry)
                self._act_tool(tid, name=rec.get("toolName"),
                               status="ok" if entry["ok"] else "error",
                               result=self._clip(self._result_text(rec.get("result")), 600),
                               ms=rec.get("durationMs"))
            elif rtype == "message_end":
                # Diagnose silent stops: log every model reply's stop reason
                # and context usage; flag anything other than a normal end.
                msg = rec.get("message") or {}
                if msg.get("role") == "assistant":
                    stop = msg.get("stopReason")
                    usage = msg.get("usage") or {}
                    log.info("agent reply: stop=%s input=%s output=%s total=%s",
                             stop, usage.get("input"), usage.get("output"),
                             usage.get("totalTokens"))
                    if stop not in ("stop", "toolUse", None):
                        err = msg.get("errorMessage")
                        self._act_note(
                            f"Modell hörte auf: {stop}"
                            + (f" – {self._clip(err, 300)}" if err else "")
                            + f" (Kontext {usage.get('input')}+{usage.get('output')} Tokens)")
            elif rtype == "auto_retry_start":
                self._act_note("Modell-Anfrage wird wiederholt …")
            elif rtype == "compaction_start":
                self._act_note("Kontext wird komprimiert …")
            elif rtype == "agent_settled":
                if rec.get("aborted"):
                    self._act_note("Lauf abgebrochen")
                break

        return {"reply": "".join(text_parts).strip(), "tool_calls": tools}

    # -- reset -------------------------------------------------------------
    def reset(self, timeout: float = 20.0) -> str:
        """Abort any running prompt and start a fresh agent session.

        Returns "new_session" or "restarted" (process restart as fallback).
        """
        self.generation += 1
        if self.alive:
            with __import__("contextlib").suppress(Exception):
                self._send({"id": f"reset-abort-{self.generation}", "type": "abort"})
        got = self._lock.acquire(timeout=timeout)
        try:
            if got and self.alive:
                while True:  # drop stale records of the aborted run
                    try:
                        self.records.get_nowait()
                    except queue.Empty:
                        break
                rid = f"reset-{self.generation}"
                self._send({"id": rid, "type": "new_session"})
                deadline = time.monotonic() + 10.0
                while time.monotonic() < deadline:
                    try:
                        rec = self.records.get(timeout=0.5)
                    except queue.Empty:
                        continue
                    if rec.get("type") == "response" and rec.get("id") == rid:
                        if rec.get("success") and not (rec.get("data") or {}).get("cancelled"):
                            self._act_clear()
                            return "new_session"
                        break
            # Fallback: hard restart of the pi process (fresh context guaranteed).
            log.warning("pi new_session failed; restarting the agent process")
            if self.proc is not None:
                self._alive = False
                with __import__("contextlib").suppress(Exception):
                    self.proc.kill()
                    self.proc.wait(timeout=5)
            self.records = queue.Queue()
            self.start()
            self._act_clear()
            return "restarted"
        finally:
            if got:
                self._lock.release()

    def _act_clear(self) -> None:
        with self._act_lock:
            self.activity = {"run": self.activity["run"] + 1, "running": False, "items": []}


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


_STT_INPUT_FORMATS = {
    "audio/webm": "matroska", "audio/ogg": "ogg", "audio/mp4": "mov",
    "audio/m4a": "mov", "audio/x-m4a": "mov", "audio/wav": "wav",
    "audio/x-wav": "wav",
}
_STT_MAX_BYTES = 8 * 1024 * 1024
_STT_MAX_SECONDS = 120


def normalize_audio(audio: bytes, content_type: str) -> bytes:
    mime = content_type.split(";", 1)[0].strip().lower()
    input_format = _STT_INPUT_FORMATS.get(mime)
    if input_format is None:
        raise ValueError("Unsupported recording format")

    # Seekable files support MP4 metadata and produce a WAV with valid lengths.
    with tempfile.TemporaryDirectory(prefix="robomotion-stt-") as tmp:
        source = Path(tmp) / "recording"
        target = Path(tmp) / "audio.wav"
        source.write_bytes(audio)
        result = subprocess.run(
            [os.environ.get("STT_FFMPEG", "ffmpeg"), "-nostdin", "-hide_banner",
             "-loglevel", "error", "-protocol_whitelist", "file",
             "-format_whitelist", input_format, "-f", input_format,
             "-i", str(source), "-map", "0:a:0", "-t", str(_STT_MAX_SECONDS + 1),
             "-ac", "1", "-ar", "16000", "-c:a", "pcm_s16le", str(target)],
            stdin=subprocess.DEVNULL, capture_output=True, timeout=20,
        )
        if result.returncode != 0:
            log.warning("FFmpeg rejected %s recording (%d bytes)", mime, len(audio))
            raise ValueError("Recording could not be decoded")
        wav = target.read_bytes()
        # Read the PCM frame count, independent of the WAV metadata size.
        with wave.open(io.BytesIO(wav), "rb") as decoded:
            frames = decoded.getnframes()
        if not frames:
            raise ValueError("Recording contains no audio")
        if frames > 16000 * _STT_MAX_SECONDS:
            raise ValueError(f"Recording exceeds {_STT_MAX_SECONDS} seconds")
        return wav


def transcribe_audio(audio: bytes) -> dict:
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
            'filename="audio.wav"\r\n'
            "Content-Type: audio/wav\r\n\r\n"
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
            "plc_mode": "sim" if IS_SIM
            else ("dry-run" if (plc and plc.config.dry_run) else "live"),
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
    generation = bridge.generation
    bg_note, bg_images = _background_for_prompt(generation)
    try:
        result = bridge.prompt(bg_note + message, images=bg_images, display=message)
    except PiAgentError as exc:
        if bg_images:
            with _bg_lock:
                if _BACKGROUND:
                    _BACKGROUND["sent"] = None  # retry with the next message
        if generation != bridge.generation:
            return jsonify({"stale": True, "reply": "", "tool_calls": []})
        log.warning("chat failed: %s", exc)
        return jsonify({"error": str(exc)}), 502
    return jsonify(result)


@app.get("/api/chat/activity")
def chat_activity() -> jsonify:
    """Live view of the agent run: thinking, tool calls (args/result), reply text."""
    if bridge is None:
        return jsonify({"run": 0, "running": False, "items": [], "generation": 0})
    return jsonify(bridge.activity_snapshot())


_reset_lock = threading.Lock()


def _robot_to_drawing_start() -> tuple[float, float]:
    """Robot part of the reset: cancel motion, clear all command flags
    (read-back), then home + jog to the drawing start (30, 35)."""
    if getattr(plc, "positioning", False):
        plc.cancel_positioning()
        deadline = time.monotonic() + 20.0
        while plc.positioning and time.monotonic() < deadline:
            time.sleep(0.1)
        if plc.positioning:
            raise RuntimeError("laufende Fahrt ließ sich nicht abbrechen")
    if IS_SIM:
        plc.reset()  # clean sim state incl. bank + trail
    else:
        plc.clear_command_flags()
    return plc.move_to_drawing_start()


def _startup_drawing_start() -> None:
    """Once at server start: drive to the drawing start like the reset
    button (robot part only). Holds the reset lock, so a concurrent reset
    gets 409; other robot POSTs get 409 while the stage is positioning.
    Faults (other than the ignored 20434) make move_to_drawing_start
    refuse to move — logged, the server keeps running."""
    with _reset_lock:
        try:
            x, z = _robot_to_drawing_start()
            log.info("startup: at drawing start (%.2f, %.2f)", x, z)
        except Exception as exc:  # noqa: BLE001
            log.warning("startup: drawing start failed: %s", exc)


@app.post("/api/reset")
def full_reset() -> jsonify:
    """Presentation reset: fresh agent session + clear route state + robot to drawing start.

    Order: stop the agent (so it cannot issue new robot commands), cancel any
    positioning/jog drawing, clear all command flags (read-back), then drive
    to the drawing start (30, 35). No server restart needed.
    """
    global _ROUTE_LAST
    if not _reset_lock.acquire(blocking=False):
        return jsonify({"error": "Reset läuft bereits"}), 409
    out: dict = {"ok": False}
    try:
        # 1) agent
        if bridge is not None:
            try:
                out["agent"] = bridge.reset()
            except Exception as exc:  # noqa: BLE001
                log.exception("agent reset failed")
                out["agent_error"] = str(exc)
        else:
            out["agent_error"] = "AI agent (pi) is not running"
        # 2) route / tracking state
        with _track_mutex:
            if "cancel" in _TRACK:
                _TRACK["cancel"].set()
        _ROUTE_LAST = {}
        with _track_mutex:
            _ROUTE_HISTORY.clear()
        _reset_tracking()
        with _track_mutex:
            out["drawing_cleared"] = len(_DRAWING["parts"])
            _DRAWING["parts"].clear()
        # 3) robot
        if plc is None or not plc_ok:
            out["robot_error"] = "PLC nicht erreichbar"
            return jsonify(out), 503
        try:
            x, z = _robot_to_drawing_start()
            out.update(ok="agent_error" not in out, x=x, z=z)
            return jsonify(out)
        except Exception as exc:  # noqa: BLE001 - reported to the UI
            log.warning("reset: drawing start failed: %s", exc)
            out["robot_error"] = str(exc)
            return jsonify(out), 500
    finally:
        _reset_lock.release()


@app.post("/api/transcribe")
def transcribe() -> jsonify:
    request.max_content_length = _STT_MAX_BYTES
    try:
        audio = request.get_data(cache=False)
    except RequestEntityTooLarge:
        return jsonify({"error": "Recording exceeds 8 MiB"}), 413
    if not audio:
        return jsonify({"error": "no audio received"}), 400
    try:
        wav = normalize_audio(audio, request.headers.get("Content-Type", "audio/webm"))
    except (ValueError, wave.Error) as exc:
        return jsonify({"error": str(exc)}), 400
    except subprocess.TimeoutExpired:
        return jsonify({"error": "Audio conversion timed out"}), 422
    except OSError as exc:
        log.error("Audio conversion unavailable: %s", exc)
        return jsonify({"error": "Audio conversion unavailable on the server"}), 503
    try:
        result = transcribe_audio(wav)
    except urllib.error.HTTPError as exc:
        exc.close()
        log.warning("STT rejected normalized WAV: HTTP %d", exc.code)
        return jsonify({"error": f"STT returned HTTP {exc.code}"}), 502
    except (urllib.error.URLError, OSError) as exc:
        log.warning("STT connection failed: %s", exc)
        return jsonify({"error": "STT server unreachable"}), 502
    return jsonify(result)


# ---- robot REST API (called by the pi extension AND available to clients) ----
def _robot_handler(fn):
    def wrapper(*args, **kwargs):
        global plc_ok
        if plc is None:
            return jsonify({"error": "PLC client not initialized"}), 503
        # A successful read can restore availability; don't resume writes
        # automatically after a disconnect.
        if not plc_ok and request.method != "GET":
            return jsonify({"error": "PLC not reachable; check status first"}), 503
        try:
            if (request.method == "POST" and getattr(plc, "positioning", False)
                    and fn.__name__ not in {"robot_drawing_start", "stop_path", "robot_wait"}):
                return jsonify({"error": "Robot is positioning/drawing by jog; use stop to cancel"}), 409
            result = fn(*args, **kwargs)
            if request.method == "GET":
                plc_ok = True
            if is_dataclass(result):
                result = asdict(result)
            return jsonify(result)
        except PlcConnectionError as exc:
            plc_ok = False
            log.warning("PLC unreachable: %s", exc)
            return jsonify({"error": f"PLC nicht erreichbar: {exc}"}), 503
        except BadNodeIdUnknown as exc:
            return jsonify({"error": f"PLC signal unavailable: {exc}"}), 501
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
    cleaned = {k: v for k, v in fields.items() if k in allowed}
    # Accept short aliases so a partial/silent write can't leave stale
    # positions behind (seen: tools posting x/z instead of x_pos/z_pos).
    if "x" in fields and "x_pos" not in cleaned:
        cleaned["x_pos"] = fields["x"]
    if "z" in fields and "z_pos" not in cleaned:
        cleaned["z_pos"] = fields["z"]
    _plc_or_none().set_profile(index, **cleaned)
    return {"ok": True, "index": index}


@app.post("/api/robot/path/start")
@_robot_handler
def robot_path_start():
    body = request.get_json(force=True, silent=True) or {}
    kwargs = {k: body[k] for k in ("profile_index", "path_vel_out", "override") if k in body}
    target = _plc_or_none()
    preview = _ROUTE_LAST
    if not (preview.get("verdict", {}).get("ok") and body.get("preview_id") == preview.get("image_id")):
        # The agent may dry-run several parts first and drive them afterwards.
        with _track_mutex:
            preview = next((r for r in reversed(_ROUTE_HISTORY)
                            if r["image_id"] == body.get("preview_id")), {})
    # Pen model for the canvas: draw=false is a travel move (pen up, no part).
    draw = body.get("draw", True) is not False
    label = body.get("label") if isinstance(body.get("label"), str) else None
    nominal = list(preview.get("plan", {}).get("nominal") or []) if preview else []
    if preview:
        pos = target.positions()
        _, x, z = preview["trajectory"][0]
        if not math.dist((pos.act_x_pos, pos.act_z_pos), (x, z)) <= 0.5:
            preview = {
                "image_id": preview["image_id"],
                "compare_unavailable": "Dry-run origin differs from the measured start position",
            }
    done_before_start = target.status().done
    if not IS_SIM and hasattr(target, "draw_by_jog"):
        # Live: g_xPathExecute is not used (no motion + warning 20434 from the
        # drawing start, 22.11.2026). The bank waypoints are driven by jog.
        waypoints = target.bank_waypoints()
        t0 = time.monotonic()
        run = _start_tracking(target, t0, preview, done_before_start, jog=True)
        part = _add_part(run, nominal or waypoints, label) if draw else None

        def _drive() -> None:
            try:
                end = target.draw_by_jog(waypoints)
                run["jog_end"] = end
                run["jog_ok"] = True
            except Exception as exc:  # noqa: BLE001 - reported via /api/robot/wait
                log.warning("jog drawing aborted: %s", exc)
                run["jog_error"] = str(exc)
            finally:
                run["jog_finished"].set()

        threading.Thread(target=_drive, daemon=True, name="jog-draw").start()
        return {"ok": True, "mode": "jog", "waypoints": waypoints,
                "part": {"id": part["id"], "label": part["label"]} if part else None}
    if draw and not nominal:
        nominal = _bank_points(target)
    t0 = time.monotonic()
    target.start_path(**kwargs)
    run = _start_tracking(target, t0, preview, done_before_start)
    part = _add_part(run, nominal, label) if draw else None
    return {"ok": True, "part": {"id": part["id"], "label": part["label"]} if part else None}


def _bank_points(target) -> list:
    """Profile-bank waypoints up to x_halt (fallback nominal for a part)."""
    pts = []
    try:
        for i in range(1, 11):
            p = target.get_profile(i)
            pts.append((p.x_pos, p.z_pos))
            if p.x_halt:
                return pts
    except Exception:  # noqa: BLE001 - nominal is cosmetic only
        log.debug("bank read for drawing part failed", exc_info=True)
    return pts


for _path, _fn in (
    ("/api/robot/path/halt", "halt_path"),
    ("/api/robot/path/hold", "hold_path"),
    ("/api/robot/path/stop", "stop_path"),
    ("/api/robot/path/interrupt", "interrupt_path"),
):
    def _make(_fn_name: str):
        def _route():
            target = _plc_or_none()
            if _fn_name == "stop_path" and getattr(target, "positioning", False):
                # The positioning routine owns the flags; it stops and clears them.
                target.cancel_positioning()
                return {"ok": True, "cancelled": "drawing-start"}
            getattr(target, _fn_name)()
            with _track_mutex:
                if "cancel" in _TRACK:
                    _TRACK["cancel"].set()
            return {"ok": True}
        _route.__name__ = _fn_name
        app.post(_path)(_robot_handler(_route))
    _make(_fn)


@app.post("/api/robot/home")
@_robot_handler
def robot_home():
    _plc_or_none().home()
    return {"ok": True}


@app.post("/api/robot/home/clear")
@_robot_handler
def robot_home_clear():
    """Reset the latching homing flag (edge-triggered plant behaviour)."""
    _plc_or_none().clear_home_flag()
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
    if cond == "done" and body.get("path_run") is True:
        with _track_mutex:
            run = _TRACK
        finished = run.get("finished")
        if finished is None:
            return {"ok": False, "condition": cond, "reason": "no tracked path run"}
        ok = finished.wait(min(600.0, max(0.0, timeout))) and run["completed"]
        if "jog_finished" in run:
            out = {"ok": ok, "condition": cond, "mode": "jog",
                   "running": not run["jog_finished"].is_set()}
            if "jog_error" in run:
                out["reason"] = run["jog_error"]
            if "jog_end" in run:
                out["end"] = [round(v, 2) for v in run["jog_end"]]
            return out
    else:
        ok = getattr(_plc_or_none(), _CONDITIONS[cond])(timeout)
    return {"ok": ok, "condition": cond}


@app.get("/api/robot/tracking")
@_robot_handler
def robot_tracking():
    """Read-only canvas state from the physical PLC, with the tracked path trail."""
    target = _plc_or_none()
    pos = target.positions()
    status = target.status()
    with _track_mutex:
        running = _TRACK["running"]
        trail = [(x, z) for _, x, z in _TRACK["samples"][-1800:]]
    return {"x": pos.act_x_pos, "z": pos.act_z_pos,
            "vx": pos.act_x_vel, "vz": pos.act_z_vel,
            "mode": "Live · Pfad" if running else "Live",
            "error": status.error, "warning": status.warning,
            "error_id": status.error_id, "trail": trail}


@app.post("/api/robot/drawing-start")
@_robot_handler
def robot_drawing_start():
    """Home, then move to the lower-left drawing corner (30, 35) without drawing.

    Live: tested sequence homing -> jog X+ -> jog Z+ (AGENTS.md "Zeichenstart").
    Blocks until arrival (~20 s); POST /api/robot/path/stop cancels it.
    """
    x, z = _plc_or_none().move_to_drawing_start()
    return {"ok": True, "x": x, "z": z}


# ---- simulator endpoints (ROBOT_BACKEND=sim only) ----------------------------
@app.get("/api/sim/state")
def sim_state():
    """Kinematic state + recorded trail for the canvas visualisation."""
    if not IS_SIM:
        return jsonify({"error": "nur im Sim-Mode (ROBOT_BACKEND=sim)"}), 400
    if plc is None:
        return jsonify({"error": "PLC client not initialized"}), 503
    pos = plc.positions()
    st = plc.status()
    return jsonify(
        {
            "x": pos.act_x_pos,
            "z": pos.act_z_pos,
            "vx": pos.act_x_vel,
            "vz": pos.act_z_vel,
            "mode": plc.mode,
            "error": st.error,
            "warning": st.warning,
            "error_id": st.error_id,
            "trail": plc.trail,
        }
    )


@app.post("/api/sim/reset")
def sim_reset():
    """Reset the simulation to a clean home state (sim only)."""
    if not IS_SIM:
        return jsonify({"error": "nur im Sim-Mode (ROBOT_BACKEND=sim)"}), 400
    if plc is None:
        return jsonify({"error": "PLC client not initialized"}), 503
    if plc.positioning:
        return jsonify({"error": "Simulator is moving to drawing start"}), 409
    plc.reset()
    return {"ok": True}


@app.post("/api/sim/speed")
def sim_speed():
    """Set the visible-simulation playback speed (sim only)."""
    if not IS_SIM:
        return jsonify({"error": "nur im Sim-Mode (ROBOT_BACKEND=sim)"}), 400
    if plc is None:
        return jsonify({"error": "PLC client not initialized"}), 503
    body = request.get_json(force=True, silent=True) or {}
    scale = plc.set_time_scale(float(body.get("time_scale", 1.0)))
    return {"ok": True, "time_scale": scale}


# ---- route planning / dry-run verification (BOTH modes) --------------------
# These endpoints NEVER drive the robot: planning is pure math, the dry run
# spins a fresh headless SimRobot, and only /api/route/actual READS the
# measured positions (10 Hz) for the plan-vs-actual comparison.
_ROUTE_IMAGES: "OrderedDict[str, bytes]" = OrderedDict()
_ROUTE_LAST: dict = {}
# All successful dry runs since the last reset, in order (replay + preview_id
# lookup). Identical waypoint sets replace their earlier entry in place.
_ROUTE_HISTORY: list[dict] = []
_ROUTE_HISTORY_MAX = 40
# Uploaded background image (e.g. photo of a workpiece), stretched over the
# safe box x 30…260 → width, z 150…35 → height. The browser normalizes it:
#   display: JPEG 920×460 for the canvas
#   grid:    PNG 920×460 with world-coordinate grid (what the agent sees)
#   rgb:     raw RGB 460×230 (baked into the dry-run preview PNGs)
# Lives until replaced/deleted; after a chat reset it is sent again.
_BACKGROUND: dict = {}
_bg_lock = threading.Lock()
_TRACK = {"running": False, "samples": []}
_track_mutex = threading.Lock()


def _reset_tracking() -> None:
    global _TRACK
    with _track_mutex:
        _TRACK = {"running": False, "samples": []}


# ---- persistent drawing (parts) -------------------------------------------
# Every path run becomes one "part" of the drawing. Parts accumulate across
# runs (a circle in 4 quarter runs stays visible as a whole) and are ONLY
# removed by an explicit delete/clear (agent tool, UI button) or the full
# presentation reset. Deleting only affects the canvas, not the stage.
_DRAWING: dict = {"parts": [], "next_id": 1}


def _add_part(run: dict, nominal: list, label: str | None) -> dict:
    with _track_mutex:
        pid = _DRAWING["next_id"]
        _DRAWING["next_id"] += 1
        part = {"id": pid, "label": (label or "").strip()[:60] or f"Teil {pid}",
                "nominal": [(float(x), float(z)) for x, z in nominal], "run": run,
                "created": time.time()}
        _DRAWING["parts"].append(part)
    return part


def _part_trail(part: dict, limit: int = 600) -> list:
    """Measured points of a part; the approach to the first waypoint is cut off."""
    run = part["run"]
    pts = list(run.get("trail_points") or ((x, z) for _, x, z in run["samples"]))
    nominal = part["nominal"]
    if nominal and pts:
        x0, z0 = nominal[0]
        first = next((i for i, (x, z) in enumerate(pts)
                      if math.dist((x, z), (x0, z0)) <= 1.5), None)
        pts = [] if first is None else pts[first:]  # start never reached: pen stayed up
    step = max(1, len(pts) // limit)
    return [(round(x, 2), round(z, 2)) for x, z in pts[::step]]


def _part_info(part: dict, with_points: bool = False) -> dict:
    run = part["run"]
    out = {"id": part["id"], "label": part["label"], "running": bool(run.get("running")),
           "completed": bool(run.get("completed")), "samples": len(run["samples"])}
    if part["nominal"]:
        out["start"] = [round(v, 2) for v in part["nominal"][0]]
        out["end"] = [round(v, 2) for v in part["nominal"][-1]]
    if with_points:
        out["trail"] = _part_trail(part)
        step = max(1, len(part["nominal"]) // 300)
        out["nominal"] = part["nominal"][::step]
    return out


@app.get("/api/drawing")
def drawing_get():
    """All parts of the current drawing (canvas layer + agent listing)."""
    points = request.args.get("points", "1") != "0"
    with _track_mutex:
        parts = [_part_info(p, points) for p in _DRAWING["parts"]]
    return jsonify({"parts": parts, "count": len(parts)})


@app.post("/api/drawing/parts/<int:part_id>/delete")
def drawing_delete_part(part_id: int):
    """Explicitly remove ONE part from the drawing (canvas only)."""
    with _track_mutex:
        part = next((p for p in _DRAWING["parts"] if p["id"] == part_id), None)
        if part is None:
            return jsonify({"error": f"Teil {part_id} existiert nicht"}), 404
        if part["run"].get("running"):
            return jsonify({"error": f"Teil {part_id} wird gerade gezeichnet"}), 409
        _DRAWING["parts"].remove(part)
        left = len(_DRAWING["parts"])
    return {"ok": True, "deleted": part_id, "label": part["label"], "remaining": left}


@app.post("/api/drawing/clear")
def drawing_clear():
    """Explicitly remove ALL parts. Requires {"confirm": true}."""
    body = request.get_json(force=True, silent=True) or {}
    if body.get("confirm") is not True:
        return jsonify({"error": 'Löschen der ganzen Zeichnung braucht {"confirm": true}'}), 400
    with _track_mutex:
        if any(p["run"].get("running") for p in _DRAWING["parts"]):
            return jsonify({"error": "Es wird gerade gezeichnet"}), 409
        n = len(_DRAWING["parts"])
        _DRAWING["parts"].clear()
    return {"ok": True, "deleted": n}


def _store_image(payload: bytes) -> str:
    img_id = uuid.uuid4().hex[:12]
    _ROUTE_IMAGES[img_id] = payload
    while len(_ROUTE_IMAGES) > 20:
        _ROUTE_IMAGES.popitem(last=False)
    return img_id


def _plan_request(body: dict):
    return plan_route(
        body.get("segments") or [],
        start=body.get("start"),
        closed=bool(body.get("closed", False)),
        vel=float(body.get("vel", 350.0)),
        acc=float(body.get("acc", 1000.0)),
    )


def _do_dry_run(body: dict) -> dict:
    plan = _plan_request(body)
    origin = body.get("origin")
    if origin is not None:
        origin = (float(origin["x"]), float(origin["z"]))
    result = simulate_path(plan.waypoints, start=origin)
    verdict = verify_route(plan, result)
    with _bg_lock:
        bg_rgb = _BACKGROUND.get("rgb")
    img_id = _store_image(render_route_view(plan, result, background=bg_rgb))
    global _ROUTE_LAST
    _ROUTE_LAST = {
        "plan": {
            "nominal": plan.nominal,
            "waypoints": plan.waypoints,
            "length": plan.length,
            "closed": plan.closed,
        },
        "trajectory": result.trajectory,
        "verdict": verdict,
        "image_id": img_id,
    }
    if verdict["ok"]:
        key = tuple((round(w["x_pos"], 2), round(w["z_pos"], 2)) for w in plan.waypoints)
        entry = {**_ROUTE_LAST, "key": key}
        with _track_mutex:
            for i, old in enumerate(_ROUTE_HISTORY):
                if old["key"] == key:
                    _ROUTE_HISTORY[i] = entry
                    break
            else:
                _ROUTE_HISTORY.append(entry)
                del _ROUTE_HISTORY[:-_ROUTE_HISTORY_MAX]
    return {
        "ok": verdict["ok"],
        "verdict": verdict,
        "duration": round(result.duration, 2),
        "wall_ms": round(result.wall_time * 1000, 1),
        "end": [round(result.end[0], 2), round(result.end[1], 2)],
        "image_id": img_id,
        "image_url": f"/api/route/preview/{img_id}.png",
        "waypoints": plan.waypoints,
    }


@app.post("/api/route/plan")
def route_plan():
    body = request.get_json(force=True, silent=True) or {}
    try:
        plan = _plan_request(body)
    except (ValueError, TypeError, KeyError, AttributeError) as exc:
        return jsonify({"error": str(exc)}), 400
    nominal = plan.nominal
    step = max(1, len(nominal) // 200)
    return jsonify({
        "ok": True,
        "length": round(plan.length, 1),
        "closed": plan.closed,
        "waypoints": plan.waypoints,
        "nominal": nominal[::step],
    })


@app.post("/api/route/simulate")
def route_simulate():
    """Plan + headless dry run + verification + rendered view."""
    body = request.get_json(force=True, silent=True) or {}
    try:
        return jsonify(_do_dry_run(body))
    except (ValueError, TypeError, KeyError, AttributeError) as exc:
        return jsonify({"error": str(exc)}), 400


@app.post("/api/route/check-image")
def route_check_image():
    """Planned route drawn sharp on the background (overview + zoom tiles).

    One call checks ALL waypoints and the straight legs between them, so
    the agent can confirm visually that the path lies on the image feature
    before dry-running. Read-only, never touches the robot.
    """
    body = request.get_json(force=True, silent=True) or {}
    with _bg_lock:
        hi = _BACKGROUND.get("rgb_hi")
        lo = _BACKGROUND.get("rgb")
    if not (hi or lo):
        return jsonify({"error": "kein Hintergrundbild hochgeladen"}), 404
    bg, size = (hi, (_BG_W * 2, _BG_H * 2)) if hi else (lo, (_BG_W, _BG_H))
    try:
        plan = _plan_request(body)
    except (ValueError, TypeError, KeyError, AttributeError) as exc:
        return jsonify({"error": str(exc)}), 400
    png, tiles = render_feature_check(plan, bg, size)
    img_id = _store_image(png)
    return jsonify({
        "ok": True,
        "image_id": img_id,
        "image_url": f"/api/route/preview/{img_id}.png",
        "resolution": "4 px/Einheit" if hi else "2 px/Einheit (altes Bild, unscharf)",
        "tiles": tiles,
        "legend": (
            "Oben Übersicht (Bahn rot, Nummern = Kacheln). Darunter je Kachel "
            "±8 Einheiten um den Mittelpunkt: feines Raster = 1 Einheit, helle "
            "Linien = Vielfache von 5; cyan = gefahrene Gerade, gelber Ring = "
            "Kachelmitte (Wegpunkt bzw. Stelle auf der Strecke). Lineale in "
            "Weltkoordinaten: z links, x unten (Striche je 1, Zahlen alle 5; "
            "Übersicht: Zahlen alle 10). Merkmal muss unter cyan/Ring liegen; "
            "Versatz am z-Lineal ablesen: z_Merkmal − z_Punkt."
        ),
    })


@app.get("/api/route/preview/<img_id>.png")
def route_preview(img_id: str):
    data = _ROUTE_IMAGES.get(img_id)
    if data is None:
        return jsonify({"error": "unknown image id"}), 404
    return app.response_class(data, mimetype="image/png")


@app.get("/api/route/last")
def route_last():
    """Last dry run (ghost overlay for the canvas + verification hints)."""
    if not _ROUTE_LAST:
        return jsonify({"has": False})
    return jsonify({"has": True, **_ROUTE_LAST})


_BG_W, _BG_H = 460, 230


def _data_url_bytes(value, mime_prefix: str) -> tuple[bytes, str]:
    """Decode 'data:<mime>;base64,<data>' and check the mime type."""
    import base64
    if not isinstance(value, str) or not value.startswith("data:") or "," not in value:
        raise ValueError("expected a base64 data URL")
    head, data = value.split(",", 1)
    mime = head[5:].split(";", 1)[0]
    if not mime.startswith(mime_prefix):
        raise ValueError(f"unexpected image type {mime!r}")
    return base64.b64decode(data, validate=True), mime


@app.post("/api/background")
def background_upload():
    """Store a browser-normalized background image (see _BACKGROUND)."""
    import base64
    body = request.get_json(force=True, silent=True) or {}
    try:
        display, display_mime = _data_url_bytes(body.get("display"), "image/")
        grid, grid_mime = _data_url_bytes(body.get("grid"), "image/png")
        rgb = base64.b64decode(body.get("rgb") or "", validate=True)
    except (ValueError, TypeError) as exc:
        return jsonify({"error": f"Bild ungültig: {exc}"}), 400
    if len(rgb) != _BG_W * _BG_H * 3:
        return jsonify({"error": "rgb muss 460×230×3 Bytes haben"}), 400
    try:
        # Optional 920×460 raw RGB for the sharp feature check (zoom tiles).
        rgb_hi = base64.b64decode(body.get("rgb_hi") or "", validate=True)
    except (ValueError, TypeError):
        rgb_hi = b""
    if len(rgb_hi) != _BG_W * 2 * _BG_H * 2 * 3:
        rgb_hi = None
    if len(display) > 8_000_000:
        return jsonify({"error": "Bild zu groß"}), 413
    name = body.get("name") if isinstance(body.get("name"), str) else "Bild"
    with _bg_lock:
        version = _BACKGROUND.get("version", 0) + 1
        _BACKGROUND.clear()
        _BACKGROUND.update(
            name=name[:120], version=version, display=display, display_mime=display_mime,
            grid=grid, grid_mime=grid_mime, rgb=rgb, rgb_hi=rgb_hi, sent=None,
            uploaded=time.time(),
        )
    log.info("background uploaded: %s (v%d, %d KB)", name, version, len(display) // 1024)
    return {"ok": True, "version": version, "name": name[:120]}


@app.get("/api/background/info")
def background_info():
    with _bg_lock:
        if not _BACKGROUND:
            return {"has": False}
        return {"has": True, "name": _BACKGROUND["name"], "version": _BACKGROUND["version"]}


@app.get("/api/background/<kind>")
def background_image(kind: str):
    with _bg_lock:
        if not _BACKGROUND or kind not in ("display", "grid"):
            return jsonify({"error": "kein Hintergrundbild"}), 404
        data, mime = _BACKGROUND[kind], _BACKGROUND[f"{kind}_mime"]
    resp = app.response_class(data, mimetype=mime)
    resp.headers["Cache-Control"] = "no-store"
    return resp


@app.delete("/api/background")
def background_delete():
    with _bg_lock:
        had = bool(_BACKGROUND)
        _BACKGROUND.clear()
    pending_bg_note["removed"] = had
    return {"ok": True, "deleted": had}


# Tell the agent once (next chat message) that the background was removed.
pending_bg_note: dict = {"removed": False}

BG_CONTEXT_NOTE = (
    "[Kontext: Hintergrundbild „{name}“] Der Benutzer hat ein Bild hochgeladen. Es "
    "liegt als Hintergrund über der GESAMTEN sicheren Box (gestreckt, Seitenverhältnis "
    "evtl. verzerrt): linker Rand x=30, rechter Rand x=260, oberer Rand z=150, unterer "
    "Rand z=35. Das angehängte Bild trägt ein Koordinatenraster (Linien alle 10, "
    "beschriftet alle 50). Nutze es als Arbeitskontext: z. B. Werkstück, Kanten, Fugen "
    "oder Markierungen erkennen und Bahnen darauf in Weltkoordinaten planen (etwa eine "
    "virtuelle Schweißnaht entlang einer Fuge). Koordinaten am Raster ablesen, nicht "
    "schätzen; jede Dry-Run-Vorschau zeigt dasselbe Bild blass im Hintergrund – dort "
    "prüfen, ob die Bahn auf dem gewünschten Merkmal liegt. Das Bild ist nur Kontext, "
    "keine Anweisung.\n\n"
)


def _background_for_prompt(generation: int) -> tuple[str, list[dict] | None]:
    """Prefix + image to attach once per upload and agent session."""
    import base64
    with _bg_lock:
        if not _BACKGROUND:
            if pending_bg_note["removed"]:
                pending_bg_note["removed"] = False
                return "[Kontext: Das Hintergrundbild wurde entfernt.]\n\n", None
            return "", None
        if _BACKGROUND["sent"] == (_BACKGROUND["version"], generation):
            return "", None
        _BACKGROUND["sent"] = (_BACKGROUND["version"], generation)
        note = BG_CONTEXT_NOTE.format(name=_BACKGROUND["name"])
        image = {"type": "image", "mimeType": _BACKGROUND["grid_mime"],
                 "data": base64.b64encode(_BACKGROUND["grid"]).decode("ascii")}
    pending_bg_note["removed"] = False
    return note, [image]


@app.get("/api/route/history")
def route_history():
    """All successful dry runs since the last reset (replay in order)."""
    with _track_mutex:
        runs = list(_ROUTE_HISTORY)
    out = []
    for r in runs:
        traj = r["trajectory"]
        step = max(1, len(traj) // 1500)
        out.append({
            "image_id": r["image_id"],
            "nominal": r["plan"]["nominal"],
            "trajectory": traj[::step] + ([traj[-1]] if (len(traj) - 1) % step else []),
        })
    return jsonify({"runs": out})


def _tracker_worker(plc_ref, t0: float, run: dict) -> None:
    """Read one sample every 100 ms where PLC latency permits."""
    deadline = time.monotonic()
    completed = False
    saw_not_done = not run["done_before_start"]
    jog_done = run.get("jog_finished")
    try:
        while not run["cancel"].is_set() and time.monotonic() - t0 < (900 if jog_done else 180):
            pos = plc_ref.positions()
            sample = (time.monotonic() - t0, float(pos.act_x_pos), float(pos.act_z_pos))
            status = plc_ref.status()
            with _track_mutex:
                run["samples"].append(sample)
            if jog_done is not None:
                # Jog drawing: the driver thread owns fault handling and ends the run.
                if jog_done.is_set():
                    completed = bool(run.get("jog_ok"))
                    break
                deadline = max(deadline + 0.1, time.monotonic())
                run["cancel"].wait(max(0, deadline - time.monotonic()))
                continue
            if status.error or status.warning or run["cancel"].is_set():
                break
            if not status.done:
                saw_not_done = True
            elif saw_not_done:
                completed = True
                break
            deadline = max(deadline + 0.1, time.monotonic())
            run["cancel"].wait(max(0, deadline - time.monotonic()))
    except Exception:
        log.warning("Position sampling ended early", exc_info=True)
    finally:
        trail = None
        if IS_SIM and not run["cancel"].is_set():  # cancelled: trail may be the next run's
            # The sim records its own fine-grained per-run trail; at 2x/5x the
            # 10 Hz samples alone would be too coarse for the drawing layer.
            try:
                trail = [(float(x), float(z)) for x, z in plc_ref.trail]
            except Exception:  # noqa: BLE001 - cosmetic
                trail = None
        with _track_mutex:
            if trail:
                run["trail_points"] = trail
            run.update(running=False, completed=completed and not run["cancel"].is_set())
            run["finished"].set()


def _start_tracking(target, t0: float, preview: dict, done_before_start: bool,
                    jog: bool = False) -> dict:
    global _TRACK
    run = {"running": True, "completed": False, "samples": [],
           "preview": preview, "done_before_start": done_before_start,
           "cancel": threading.Event(), "finished": threading.Event()}
    if jog:
        run["jog_finished"] = threading.Event()
    with _track_mutex:
        if "cancel" in _TRACK:
            _TRACK["cancel"].set()
        _TRACK = run
    threading.Thread(target=_tracker_worker, args=(target, t0, run),
                     daemon=True, name="route-tracker").start()
    return run


@app.get("/api/route/actual")
def route_actual():
    """Measured samples of the last run + plan-vs-actual comparison."""
    with _track_mutex:
        samples = list(_TRACK["samples"])
        running = _TRACK["running"]
        completed = _TRACK.get("completed", False)
        preview = _TRACK.get("preview", {})
    out: dict = {"running": running, "completed": completed, "n": len(samples),
                 "preview_id": preview.get("image_id"), "samples": samples}
    if "compare_unavailable" in preview:
        out["compare_unavailable"] = preview["compare_unavailable"]
    traj = preview.get("trajectory")
    if completed and len(samples) >= 2 and traj:
        sim_res = SimResult(
            trajectory=traj, nominal=[], done=True, error=False,
            error_id=0, events=[], end=(traj[-1][1], traj[-1][2]),
            duration=traj[-1][0], wall_time=0.0,
        )
        out["compare"] = compare_run(
            sim_res, [(t, x, z) for t, x, z in samples])
    return jsonify(out)


# ---------------------------------------------------------------------------
# startup / shutdown
# ---------------------------------------------------------------------------
def main() -> int:
    global plc, plc_ok, bridge

    host = os.environ.get("WEB_HOST", "0.0.0.0")
    port = int(os.environ.get("WEB_PORT", "8080"))
    tls_cert = os.environ.get("WEB_TLS_CERT")
    tls_key = os.environ.get("WEB_TLS_KEY")
    tls_ca = os.environ.get("WEB_TLS_CA")
    if bool(tls_cert) != bool(tls_key) or (tls_ca and not tls_cert):
        log.error("WEB_TLS_CERT and WEB_TLS_KEY must be set together; WEB_TLS_CA requires both")
        return 2

    tls_context = None
    agent_tls_env = {}
    try:
        if tls_cert and tls_key:
            tls_context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
            tls_context.load_cert_chain(tls_cert, tls_key)
        if tls_ca:
            ca_path = Path(tls_ca).resolve()
            ssl.create_default_context(cafile=str(ca_path))
            agent_tls_env["NODE_EXTRA_CA_CERTS"] = str(ca_path)
    except (OSError, ValueError) as exc:
        log.error("Invalid web TLS configuration: %s", exc)
        return 2

    scheme = "https" if tls_context else "http"
    # Default to this server's protocol and port, including in simulator runs.
    callback_url = os.environ.get("PI_WEB_APP_URL", f"{scheme}://127.0.0.1:{port}")

    config = PlcConfig.from_env()
    log.info("PLC config: %s", config.describe())
    if IS_SIM:
        log.info("ROBOT_BACKEND=sim → in-process SimRobot (no plant contact)")
    plc = SyncPlc(config, connect=False, robot_cls=SimRobot if IS_SIM else None)
    try:
        plc.connect(timeout=15)
        plc_ok = True
        log.info("PLC session established")
        if os.environ.get("STARTUP_DRAWING_START", "1") != "0":
            log.info("startup: driving to drawing start (STARTUP_DRAWING_START=0 disables)")
            threading.Thread(target=_startup_drawing_start, daemon=True,
                             name="startup-drawing-start").start()
    except Exception as exc:  # noqa: BLE001 - app must survive without the plant
        plc_ok = False
        log.warning("PLC not reachable at startup: %s (robot API will return 503)", exc)

    bridge = PiBridge(
        AGENT_DIR,
        env={
            **agent_tls_env,
            "PI_WEB_APP_URL": callback_url,
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

    log.info("web app listening on %s://%s:%d", scheme, host, port)
    try:
        app.run(host=host, port=port, threaded=True, ssl_context=tls_context)
    finally:
        if bridge is not None:
            bridge.stop()
        if plc is not None:
            plc.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
