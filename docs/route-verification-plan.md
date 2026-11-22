# Routenplanung mit Probelauf-Verifikation (Plan)

Entstand aus der Kreis-Qualitätsrunde (21./22.11.2026). Ziel: **kein
formspezifisches Tooling**, sondern ein allgemeiner Loop, der Fehler
klassenweit fängt — gesteuert über Prompt-Guides, mit zwei Kernfunktionen:
**Routenplanung** und **Selbstverifikation**, plus **Vision als Feedback**
der Planung.

## Architektur

```
Agent: plan_route(spec) ──► simulate_path()        ← headless, frische SimRobot-Instanz,
                               │                     Fast-Forward, berührt den Roboter NIE
                               ├─► verify_route()   ← generische Metriken (jede Form)
                               └─► render_route_view() ─► PNG (Vision-Feedback, qwen ist VL-fähig)
                    fail ──► Spec anpassen (mehr Punkte / kleinerer r / geringere vel) ──► erneut (max. 3×)
                    pass ──► set_path_waypoints + start_path_run (echter Lauf)
                             └─► compare_run()  ← Plan vs. Ist (10-Hz-Sampling)
```

## Bausteine

1. **`sps_client/simulator.py`**
   - Interne Simulationsuhr (`_clock`) statt `time.monotonic()` →
     `_step` wandzeitunabhängig; 8-s-Schutzreaktion gilt in Sim-Zeit.
   - `SimRobot.time_scale` (1× = Anlagentempo, bis 10×) für die sichtbare
     Simulation (`_tick_loop` rechnet `dt * time_scale`).
   - `simulate_path(waypoints, start, dt=1/30, max_t=120) -> SimResult`
     (modulübergreifend): frische Instanz, gleiche `start_path`-Logik
     (gefactored in `_prepare_path_run`), synchrone `_step`-Schleife.
   - `SimResult`: zeitgestempelte Trajektorie `(t,x,z)`, nominelle Bahn
     (expandiert inkl. Blending), done/error/events, Endposition,
     Sim-Dauer, Wall-Time.

2. **`sps_client/route.py`** (nur Stdlib)
   - `plan_route(segments, start, closed, vel, acc) -> RoutePlan`
     - Segmente: `line {to}`, `arc {center, sweep_deg, ccw}`,
       `polyline {points}` — die einzigen drei Grundformen.
     - Dichte nominelle Bahn (~2-Einheiten-Resampling) + ≤ 10
       Wegpunkt-Slots (gleichmäßige Bogenverteilung) + automatische
       `blending_radius`-Berechnung pro Ecke (Krümmungsbasiert;
       Kreis → ≈ 0,31·r als Nebenprodukt).
     - Pre-Checks mit klaren Fehlern: nominelle Bahn in sicherer Box,
       Slot-Budget, minimale Größe. (Nicht nur die Grobpunkte, sondern
       die ganze nominelle Bahn wird gegen die Box geprüft.)
   - `verify_route(plan, result) -> {ok, metrics, reasons}` — generische
     Invarianten, identisch für jede Form:
     | Check | fängt |
     |---|---|
     | `completed` | Hänge-Loops, Early-Stopp, Creep |
     | `clean` (Events/ErrorID) | 21005, Grenzverletzung |
     | `end_error` (Tol. inkl. Schließungsecke) | falscher Endpunkt, offene Form |
     | `max_dev`/`mean_dev` (Trail → nominell, Anfahrt ausgeschlossen) | Ei-Form, Eckenschnitt, Drift |
     | `coverage` (nominell → Trail) | ausgelassene Bahnabschnitte |
     | `in_box` | Ausreißer |
     | `monotonic` (Projektion s(t)) | Spiralen, Pendeln |
     - Reasons mit Ort und Wert → Agent kann gezielt replannen.
   - `interpolate(traj, t)` — Linear-Interpolation der Sim-Trajektorie.
   - `compare_run(plan, sim_traj, actual)` — Plan vs. Ist: räumlich,
     zeitlich (`duration_ratio` = messbarer Kalibrierfaktor), nur wenn
     Ist-Samples vorliegen.
   - `render_route_view(plan, result) -> PNG-Bytes` — eigener PNG-Encoder
     (zlib/stdlib, keine Bildbibliothek): Box, nominelle Bahn (grau),
     Trail (blau), Abweichungen (rot), Start/End-Marker. 460×230.

3. **`web-app/main.py`** — Endpunkte in BEIDEN Modi (live + sim),
   berühren `plc` nie (nur lesen für Ist-Sampling):
   - `POST /api/route/plan`
   - `POST /api/route/simulate` → Plan + headless-Sim + Verifikation →
     Metriken + `image_id`
   - `GET /api/route/preview/<id>.png` (In-Memory-Store, letzte ~20)
   - `GET /api/route/last` → letzter Probelauf (Ghost-Overlay fürs Canvas)
   - `GET /api/route/actual` → Ist-Samples + `compare_run`-Metriken
   - `POST /api/sim/speed` → `time_scale` (sim-only)
   - Der geometrische `start` ist der Bahnbeginn. `origin: {x,z}` ist
     optional und bestimmt die simulierte Zufahrt (Standard: Sim-Home).
     Beide Dry-Run-Endpunkte funktionieren auch ohne SPS-Verbindung.
   - **Ist-Sampling 10 Hz** (lesend, sofern die Antwortzeiten es zulassen):
     startet bei `/api/robot/path/start`, endet bei Abschluss, Störung,
     Abbruch oder nach 180 s. `preview_id` ordnet die Messung einem Dry-Run zu.

4. **`web-app/agent/.pi/extensions/robot.ts`**
   - Provider `input: ["text", "image"]` (qwen ist Vision-fähig).
   - Neue Tools: `plan_route`, `simulate_route` (Text-Metriken **und**
     PNG als Image-Content), `compare_run`.
   - `set_path_waypoints` erhält nur einen **Hinweis**
     `dry_run_verified: true/false` (Wegepunkte stimmen mit dem letzten
     bestandenen Probelauf überein?) — **kein hartes Gate**, die
     Steuerung läuft über den Prompt.

5. **`web-app/agent/AGENTS.md`** — Pflicht-Loop als eigene Sektion:
   1. Status lesen → 2. `plan_route` → 3. `simulate_route` →
   4. Metriken **und** Bild prüfen (Checkliste: Form erkennbar?
   geschlossen? Ecken nicht geschnitten? Ausbauchungen? Zufahrt sauber?
   in der Box?) → 5. bei fail: Spec anpassen, max. 3 Durchgänge, dann
   ehrlich melden → 6. erst dann fahren → 7. nach dem Lauf
   `compare_run`/Endposition prüfen.
   Kreis-Formel und Blend-Faustregel entfallen (stecken jetzt in
   `plan_route`).

6. **`web-app/templates/index.html`** — Ghost-Preview des letzten
   Probelaufs (gestrichelt) + Tempo-Auswahl 1×/2×/5×.

## Festhaltene Entscheidungen

- **Vision:** lokales qwen via vLLM, `input: ["text","image"]`; Bild
  klein halten (~460×230) wegen 32k-Kontext.
- **Gate:** KEIN hartes Gate in `set_path_waypoints`; starker
  Prompt-Steering + `dry_run_verified`-Hinweis.
- **Simulationstempo:** Probelauf = Fast-Forward (headless, keine
  Wandzeit); Sichtbare Sim = interpoliert abspielbare Trajektorie mit
  `time_scale` (1× ≈ Anlagentempo; Kalibrierfaktor über
  `duration_ratio` aus `compare_run` messbar).
- **Ist-Sampling:** 10 Hz, nur lesend, Hintergrund-Thread.
- **Slot-Budget:** ≤ 10 Wegepunkte (Profilbank), Verteilung automatisch.

## Tests

- `sps-client/tests/test_route.py` (Plain-Python, kein Framework):
  Formen-Batterie — Linie, geschlossenes Polygon, Kreis, Bogen,
  Factory-Arc + Negativfall (Box-Verletzung muss `RoutePlanningError`
  werfen) + headless-Sim-Smoke-Test.
- Regression: Chat „Fahr einen Kreis.“ + Case-2-Prompt, Screenshot
  1280×800, `.playwright-mcp/` danach löschen.
