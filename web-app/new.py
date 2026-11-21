#!/usr/bin/env python3
"""
Voice-to-Text web app for Ollama (Qwen3-ASR). Zero pip dependencies.

Run:   python stt_webapp.py            -> http://localhost:8080
       python stt_webapp.py 9000       -> custom port
"""
import base64
import html
import json
import sys
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

# ======================= CONFIG =======================
API_BASE = "http://localhost:11434"
MODEL = "frozenlab/qwen3-asr:1.7b"
API_KEY = ""
LANGUAGE = "DE"  # "de" = German, "en" = English, "" = auto-detect
# ======================================================



PAGE = r"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<link rel="stylesheet" href="styles.css">
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

  <p style="color:var(--muted);font-size:.85rem;margin:0 0 16px">Endpoint: <code>__ENDPOINT__</code><br>Model: <code>__MODEL__</code></p>

  <div class="controls">
    <button id="rec">● Aufnahme Starten</button>


<label for="file-upload" class="button">Choose File</label>
<input type="file" id="file-upload" hidden>


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

async function blobTo16kWav(blob) {
  const arrayBuf = await blob.arrayBuffer();
  const audioCtx = new (window.AudioContext || window.webkitAudioContext)();
  const decoded = await audioCtx.decodeAudioData(arrayBuf);
  const targetRate = 16000;
  const offlineCtx = new OfflineAudioContext(1, Math.ceil(decoded.duration * targetRate), targetRate);
  const source = offlineCtx.createBufferSource();
  source.buffer = decoded;
  source.connect(offlineCtx.destination);
  source.start(0);
  const rendered = await offlineCtx.startRendering();
  const samples = rendered.getChannelData(0);

  const buffer = new ArrayBuffer(44 + samples.length * 2);
  const view = new DataView(buffer);
  const writeStr = (offset, str) => { for (let i = 0; i < str.length; i++) view.setUint8(offset + i, str.charCodeAt(i)); };

  writeStr(0, 'RIFF');
  view.setUint32(4, 36 + samples.length * 2, true);
  writeStr(8, 'WAVE');
  writeStr(12, 'fmt ');
  view.setUint32(16, 16, true);
  view.setUint16(20, 1, true);
  view.setUint16(22, 1, true);
  view.setUint32(24, targetRate, true);
  view.setUint32(28, targetRate * 2, true);
  view.setUint16(32, 2, true);
  view.setUint16(34, 16, true);
  writeStr(36, 'data');
  view.setUint32(40, samples.length * 2, true);

  let offset = 44;
  for (let i = 0; i < samples.length; i++, offset += 2) {
    const s = Math.max(-1, Math.min(1, samples[i]));
    view.setInt16(offset, s < 0 ? s * 0x8000 : s * 0x7FFF, true);
  }
  await audioCtx.close();
  return new Blob([buffer], { type: 'audio/wav' });
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
  recorder = new MediaRecorder(stream);
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
  const rawBlob = new Blob(chunks, { type: recorder.mimeType || "audio/webm" });
  if (Date.now() - t0 < 300 || rawBlob.size < 500) { setStatus("Too short, nothing sent"); return; }
  setStatus("Converting & transcribing...");
  $("rec").disabled = true;
  try {
    const wavBlob = await blobTo16kWav(rawBlob);
    const res = await fetch("/transcribe", {
      method: "POST",
      headers: { "Content-Type": "audio/wav" },
      body: wavBlob,
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
    if base.endswith("/v1"):
        base = base[:-3]
    if not base.endswith("/api/chat"):
        base += "/api/chat"
    return base


def forward(base_url, model, api_key, language, audio_wav):
    lang_name = "De"
    prompt = f"language {lang_name}<asr_text>" if lang_name else ""

    payload = {
        "model": model,
        "stream": False,
        "messages": [
            {
                "role": "user",
                "content": prompt,
                "images": [base64.b64encode(audio_wav).decode("ascii")],
            }
        ],
    }

    headers = {"Content-Type": "application/json"}
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"

    req = urllib.request.Request(
        build_endpoint(base_url),
        data=json.dumps(payload).encode("utf-8"),
        headers=headers,
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=300) as resp:
        raw = json.loads(resp.read().decode("utf-8", "replace"))

    text = raw.get("message", {}).get("content", "")
    if "<asr_text>" in text:
        text = text.split("<asr_text>", 1)[1]
    return {"text": text.strip()}


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
            page = (
                PAGE.replace("__ENDPOINT__", html.escape(build_endpoint(API_BASE)))
                .replace("__MODEL__", html.escape(MODEL))
            )
            self._send(200, page, "text/html")
        else:
            self._send(404, {"error": "not found"})

    def do_POST(self):
        if self.path != "/transcribe":
            self._send(404, {"error": "not found"})
            return
        try:
            length = int(self.headers.get("Content-Length", 0))
            audio = self.rfile.read(length)
            result = forward(API_BASE, MODEL, API_KEY.strip(), LANGUAGE.strip(), audio)
            self._send(200, result)
        except urllib.error.HTTPError as e:
            detail = e.read().decode("utf-8", "replace")[:300]
            self._send(502, {"error": f"Upstream HTTP {e.code}: {detail}"})
        except Exception as e:
            self._send(502, {"error": str(e)})

    def log_message(self, fmt, *args):
        pass


if __name__ == "__main__":
    port = int(sys.argv[1]) if len(sys.argv) > 1 else 8080
    host = sys.argv[2] if len(sys.argv) > 2 else "127.0.0.1"
    server = ThreadingHTTPServer((host, port), Handler)
    print(f"Voice to Text running at http://{'localhost' if host == '127.0.0.1' else host}:{port}  (Ctrl+C to stop)")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
