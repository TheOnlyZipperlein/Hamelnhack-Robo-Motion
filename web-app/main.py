#!/usr/bin/env python3
"""
Voice-to-Text web app. Zero pip dependencies (Python standard library only).

Config: edit API_BASE (and optionally MODEL / API_KEY / LANGUAGE) below.

Run:   python stt_webapp.py            -> http://localhost:8080
       python stt_webapp.py 9000       -> custom port
       python stt_webapp.py 8080 0.0.0.0  -> reachable from other devices
                                             (mic then needs HTTPS, see notes)

The browser records the mic and sends the audio to this tiny server, which
forwards it to your OpenAI-compatible /v1/audio/transcriptions endpoint.
Going through the server avoids CORS problems with your IP/Ollama-style host.
"""
import html
import json
import sys
import uuid
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
#Qwen3-ASR-1.7B


# ======================= CONFIG =======================
# Normally you only need to change API_BASE.
API_BASE = "http://10.30.35.110:4001/v1"  # IP:PORT of your OpenAI-compatible server (http:// and /v1 optional)
MODEL = "qwen"                       # model name your server expects 
API_KEY = ""                              # leave empty for local servers
LANGUAGE = "de"                             # e.g. "de"; empty = auto-detect
# ======================================================

PAGE = r"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Voice to Text</title>
<style>
  :root { --bg:#fff; --fg:#1d1d1f; --muted:#6b6b70; --card:#f4f4f6; --line:#d8d8de; --accent:#2f6fed; --rec:#d93025; }
  @media (prefers-color-scheme: dark) {
    :root { --bg:#17181b; --fg:#ececf0; --muted:#9a9aa3; --card:#222328; --line:#35363d; --accent:#6c9bff; --rec:#ff6b5e; }
  }
  * { box-sizing: border-box; }
  body { margin:0; font:16px/1.5 system-ui,-apple-system,"Segoe UI",sans-serif; background:var(--bg); color:var(--fg); }
  main { max-width:720px; margin:0 auto; padding:24px 16px 48px; }
  h1 { font-size:1.3rem; margin:0 0 16px; }
  details { background:var(--card); border:1px solid var(--line); border-radius:10px; padding:10px 14px; margin-bottom:20px; }
  summary { cursor:pointer; font-weight:600; }
  .grid { display:grid; grid-template-columns:1fr 1fr; gap:10px 12px; margin-top:12px; }
  .grid .full { grid-column:1 / -1; }
  label { display:block; font-size:.8rem; color:var(--muted); margin-bottom:2px; }
  input { width:100%; padding:8px 10px; border:1px solid var(--line); border-radius:8px; background:var(--bg); color:var(--fg); font:inherit; }
  .controls { display:flex; align-items:center; gap:12px; flex-wrap:wrap; }
  #rec { padding:12px 22px; border:0; border-radius:999px; background:var(--accent); color:#fff; font:inherit; font-weight:600; cursor:pointer; }
  #rec.on { background:var(--rec); animation:pulse 1.2s infinite; }
  #rec:disabled { opacity:.5; cursor:wait; }
  @keyframes pulse { 50% { box-shadow:0 0 0 8px color-mix(in srgb, var(--rec) 25%, transparent); } }
  button.ghost { padding:8px 14px; border:1px solid var(--line); border-radius:8px; background:var(--card); color:var(--fg); font:inherit; cursor:pointer; }
  #status { color:var(--muted); font-size:.9rem; margin:12px 0; min-height:1.4em; }
  #status.err { color:var(--rec); }
  textarea { width:100%; min-height:260px; padding:12px; border:1px solid var(--line); border-radius:10px; background:var(--bg); color:var(--fg); font:inherit; resize:vertical; }
  .row { display:flex; gap:8px; margin-top:10px; }
</style>
</head>
<body>
<main>
  <h1>Voice to Text</h1>

  <p style="color:var(--muted);font-size:.85rem;margin:0 0 16px">Endpoint: <code>__ENDPOINT__</code><br>Change <code>API_BASE</code> at the top of stt_webapp.py.</p>

  <div class="controls">
    <button id="rec">● Start recording</button>
    <span id="timer"></span>
  </div>
  <div id="status">Ready</div>

  <textarea id="out" placeholder="Transcript appears here..."></textarea>
  <div class="row">
    <button class="ghost" id="copy">Copy</button>
    <button class="ghost" id="clear">Clear</button>
  </div>
</main>

<script>
const $ = id => document.getElementById(id);
let recorder = null, chunks = [], stream = null, t0 = 0, tick = null;

function setStatus(msg, err = false) { $("status").textContent = msg; $("status").className = err ? "err" : ""; }

function pickMime() {
  const opts = ["audio/webm;codecs=opus", "audio/webm", "audio/ogg;codecs=opus", "audio/mp4"];
  return opts.find(m => window.MediaRecorder && MediaRecorder.isTypeSupported(m)) || "";
}

async function start() {
  if (!navigator.mediaDevices?.getUserMedia) {
    setStatus("Microphone not available. Use http://localhost or HTTPS.", true);
    return;
  }
  try {
    stream = await navigator.mediaDevices.getUserMedia({ audio: true });
  } catch (e) {
    setStatus("Mic error: " + e.message, true);
    return;
  }
  chunks = [];
  const mime = pickMime();
  recorder = new MediaRecorder(stream, mime ? { mimeType: mime } : undefined);
  recorder.ondataavailable = e => { if (e.data.size) chunks.push(e.data); };
  recorder.onstop = send;
  recorder.start();
  t0 = Date.now();
  tick = setInterval(() => $("timer").textContent = ((Date.now() - t0) / 1000).toFixed(1) + " s", 100);
  $("rec").textContent = "■ Stop & transcribe";
  $("rec").classList.add("on");
  setStatus("Recording...");
}

function stop() {
  clearInterval(tick);
  $("timer").textContent = "";
  recorder.stop();
  stream.getTracks().forEach(t => t.stop());
  $("rec").classList.remove("on");
  $("rec").textContent = "● Start recording";
}

async function send() {
  const type = recorder.mimeType || "audio/webm";
  const blob = new Blob(chunks, { type });
  if (Date.now() - t0 < 300 || blob.size < 500) { setStatus("Too short, nothing sent"); return; }
  setStatus("Transcribing...");
  $("rec").disabled = true;
  try {
    const res = await fetch("/transcribe", {
      method: "POST",
      headers: { "Content-Type": type },
      body: blob,
    });
    const data = await res.json();
    if (!res.ok || data.error) throw new Error(data.error || ("HTTP " + res.status));
    const text = (data.text || "").trim();
    if (text) {
      $("out").value += ($("out").value && !$("out").value.endsWith("\n") ? "\n" : "") + text + "\n";
      $("out").scrollTop = $("out").scrollHeight;
    }
    setStatus(text ? "Done" : "Done (no speech detected)");
  } catch (e) {
    setStatus("Error: " + e.message, true);
  } finally {
    $("rec").disabled = false;
  }
}

$("rec").onclick = () => (recorder && recorder.state === "recording") ? stop() : start();
$("copy").onclick = async () => {
  try { await navigator.clipboard.writeText($("out").value.trim()); setStatus("Copied"); }
  catch { $("out").select(); document.execCommand("copy"); setStatus("Copied"); }
};
$("clear").onclick = () => { $("out").value = ""; setStatus("Ready"); };
</script>
</body>
</html>
"""


def build_endpoint(base):
    base = base.strip().rstrip("/")
    if not base.startswith(("http://", "https://")):
        base = "http://" + base
    if base.endswith("/audio/transcriptions"):
        return base
    if not base.endswith("/v1"):
        base += "/v1"
    return base + "/audio/transcriptions"


def ext_for(content_type):
    ct = content_type.lower()
    if "webm" in ct:
        return "webm"
    if "ogg" in ct:
        return "ogg"
    if "mp4" in ct or "m4a" in ct:
        return "m4a"
    if "wav" in ct:
        return "wav"
    return "webm"


def forward(base_url, model, api_key, language, audio, content_type):
    boundary = uuid.uuid4().hex
    parts = []

    def field(name, value):
        parts.append(
            f'--{boundary}\r\nContent-Disposition: form-data; name="{name}"\r\n\r\n{value}\r\n'.encode()
        )

    field("model", model)
    field("response_format", "json")
    if language:
        field("language", language)
    parts.append(
        (
            f'--{boundary}\r\nContent-Disposition: form-data; name="file"; '
            f'filename="audio.{ext_for(content_type)}"\r\n'
            f"Content-Type: {content_type.split(';')[0]}\r\n\r\n"
        ).encode()
    )
    parts.append(audio)
    parts.append(f"\r\n--{boundary}--\r\n".encode())

    headers = {"Content-Type": f"multipart/form-data; boundary={boundary}"}
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"

    req = urllib.request.Request(
        build_endpoint(base_url), data=b"".join(parts), headers=headers, method="POST"
    )
    with urllib.request.urlopen(req, timeout=300) as resp:
        raw = resp.read().decode("utf-8", "replace")
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        return {"text": raw}  # some servers return plain text


class Handler(BaseHTTPRequestHandler):
    def _send(self, code, body, ctype="application/json"):
        if isinstance(body, (dict, list)):
            body = json.dumps(body).encode("utf-8")
        elif isinstance(body, str):
            body = body.encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", ctype + "; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        if self.path in ("/", "/index.html"):
            self._send(200, PAGE.replace("__ENDPOINT__", html.escape(build_endpoint(API_BASE))), "text/html")
        else:
            self._send(404, {"error": "not found"})

    def do_POST(self):
        if self.path != "/transcribe":
            self._send(404, {"error": "not found"})
            return
        try:
            length = int(self.headers.get("Content-Length", 0))
            audio = self.rfile.read(length)
            result = forward(
                API_BASE, MODEL, API_KEY.strip(), LANGUAGE.strip(), audio,
                self.headers.get("Content-Type", "audio/webm"),
            )
            self._send(200, result)
        except urllib.error.HTTPError as e:
            detail = e.read().decode("utf-8", "replace")[:300]
            self._send(502, {"error": f"Upstream HTTP {e.code}: {detail}"})
        except Exception as e:
            self._send(502, {"error": str(e)})

    def log_message(self, fmt, *args):
        pass  # quiet


if __name__ == "__main__":
    port = int(sys.argv[1]) if len(sys.argv) > 1 else 8080
    host = sys.argv[2] if len(sys.argv) > 2 else "127.0.0.1"
    server = ThreadingHTTPServer((host, port), Handler)
    print(f"Voice to Text running at http://{'localhost' if host == '127.0.0.1' else host}:{port}  (Ctrl+C to stop)")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
