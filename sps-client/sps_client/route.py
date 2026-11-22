"""Shape-agnostic route planning, verification and rendering (stdlib only).

This is the agent's GENERAL workflow backbone — no per-shape tooling:

    plan_route(segments)          -> RoutePlan (<=10 slots, auto-blending)
    simulate_path(waypoints)      -> SimResult  (headless fast-forward,
                                         the robot is never touched)
    verify_route(plan, result)    -> {ok, metrics, reasons} (generic
                                         invariants, identical for every shape)
    render_route_view(plan,res)   -> PNG bytes (vision feedback)
    compare_run(result, actual)   -> plan vs. measured 10 Hz samples

Segments come from three primitives only: ``line``, ``arc`` (a full
circle is an arc with sweep 360) and ``polyline``.
"""

from __future__ import annotations

import math
import struct
import zlib
from dataclasses import dataclass

from .simulator import SIM_HOME, SIM_SAFE_BOX, SimResult

__all__ = [
    "RoutePlanningError",
    "RoutePlan",
    "plan_route",
    "verify_route",
    "compare_run",
    "interpolate",
    "render_route_view",
    "render_feature_check",
]

SAFE = SIM_SAFE_BOX
_SAMPLE = 2.0           # nominal resampling spacing (units)
_MAX_SLOTS = 10         # profile bank size


class RoutePlanningError(ValueError):
    """Route spec cannot be planned (message states why)."""


@dataclass
class RoutePlan:
    """A planned route: dense nominal path + <=10 profile waypoints."""

    nominal: list[tuple[float, float]]
    waypoints: list[dict]
    length: float
    closed: bool


# ------------------------------------------------------------------ planning
def _inside(x: float, z: float) -> bool:
    return (SAFE["x_min"] - 1e-8 <= x <= SAFE["x_max"] + 1e-8
            and SAFE["z_min"] - 1e-8 <= z <= SAFE["z_max"] + 1e-8)


def _point(value) -> tuple[float, float]:
    x, z = (value["x"], value["z"]) if isinstance(value, dict) else value
    pt = (float(x), float(z))
    if not _inside(*pt):
        raise RoutePlanningError(f"Punkt {pt} liegt außerhalb der sicheren Box")
    return pt


def _build_nominal(segments: list[dict], start: tuple[float, float], *,
                   arc_steps: list[int] | None = None) -> list[tuple[float, float]]:
    """Walk the segments and collect the raw (sparse) intended path."""
    pts: list[tuple[float, float]] = [start]
    cur = start
    arcs = iter(arc_steps) if arc_steps is not None else None
    for seg in segments:
        kind = seg.get("kind")
        if kind == "line":
            cur = _point(seg["to"])
            pts.append(cur)
        elif kind == "arc":
            cx, cz = _point(seg["center"])
            sweep = float(seg.get("sweep_deg", 360.0))
            if not 0 < sweep <= 360:
                raise RoutePlanningError("Bogenwinkel muss größer als 0 und höchstens 360° sein")
            ccw = bool(seg.get("ccw", True))
            r = math.hypot(cur[0] - cx, cur[1] - cz)
            if r < 1e-6:
                raise RoutePlanningError("arc: Startpunkt liegt im Zentrum")
            a0 = math.atan2(cur[1] - cz, cur[0] - cx)
            a1 = a0 + (1.0 if ccw else -1.0) * math.radians(sweep)
            for quadrant in range(-6, 7):
                a = quadrant * math.pi / 2
                if min(a0, a1) <= a <= max(a0, a1):
                    _point((cx + r * math.cos(a), cz + r * math.sin(a)))
            steps = (next(arcs) if arcs is not None
                     else max(2, math.ceil(abs(a1 - a0) * r / _SAMPLE)))
            for k in range(1, steps + 1):
                a = a0 + (a1 - a0) * k / steps
                cur = (cx + r * math.cos(a), cz + r * math.sin(a))
                pts.append(cur)
        elif kind == "polyline":
            points = seg.get("points", [])
            if len(points) > _MAX_SLOTS:
                raise RoutePlanningError(
                    "Zu viele Polygonpunkte für die Profilbank (max. 10 inkl. "
                    "Start) — Bahn in mehrere Läufe teilen; der nächste Lauf "
                    "startet am letzten Punkt des vorigen")
            for p in points:
                cur = _point(p)
                pts.append(cur)
        else:
            raise RoutePlanningError(
                f"unbekanntes Segment {kind!r} (erlaubt: line, arc, polyline)"
            )
    return pts


def _resample(pts: list[tuple[float, float]], spacing: float = _SAMPLE) -> list[tuple[float, float]]:
    """Evenly resample a sparse polyline (endpoint-preserving)."""
    if len(pts) < 2:
        raise RoutePlanningError("Weg zu kurz (< 2 Punkte)")
    out: list[tuple[float, float]] = [pts[0]]
    for (ax, az), (bx, bz) in zip(pts, pts[1:]):
        length = math.hypot(bx - ax, bz - az)
        if length < 1e-9:
            continue
        n = math.ceil(length / spacing)
        out.extend((ax + (bx - ax) * i / n, az + (bz - az) * i / n)
                   for i in range(1, n))
        out.append((bx, bz))
    return out


def plan_route(
    segments: list[dict],
    start: tuple[float, float] | None = None,
    *,
    closed: bool = False,
    vel: float = 350.0,
    acc: float = 1000.0,
) -> RoutePlan:
    """Plan a route from primitive segments.

    Produces a dense NOMINAL path (the intent — verification reference)
    and at most 10 profile waypoints with automatic per-corner blending.
    Raises :class:`RoutePlanningError` with a reason when the route is
    invalid (safe-box violation, slot budget, degenerate geometry).
    """
    if not segments:
        raise RoutePlanningError("mind. ein Segment erforderlich")
    if not all(math.isfinite(v) and 0 < v <= 10000 for v in (vel, acc)):
        raise RoutePlanningError("vel und acc müssen endlich und positiv sein (max. 10000)")
    if len(segments) > 10 or any(not isinstance(seg, dict) for seg in segments):
        raise RoutePlanningError("Maximal 10 gültige Segmente erlaubt")
    if start is None:
        first = segments[0]
        if first.get("kind") == "line":
            start = _point(first["to"])
            segments = segments[1:]
        elif first.get("kind") == "polyline" and first.get("points"):
            start = _point(first["points"][0])
            segments = [{**first, "points": first["points"][1:]}, *segments[1:]]
        else:
            raise RoutePlanningError("Ein Bogen als erstes Segment benötigt einen Startpunkt")
    intended_rough = _build_nominal(segments, _point(start))
    if len(intended_rough) >= 3 and math.dist(intended_rough[-1], intended_rough[0]) < 1e-6:
        closed = True
        intended_rough[-1] = intended_rough[0]
    elif closed:
        raise RoutePlanningError("Geschlossene Bahn benötigt einen expliziten Rückweg zum Startpunkt")
    intended_dense = _resample(intended_rough)
    length = sum(
        math.hypot(p[0][0] - p[1][0], p[0][1] - p[1][1])
        for p in zip(intended_dense, intended_dense[1:])
    )
    if length < 1.0:
        raise RoutePlanningError("Bahn zu kurz (< 1 Einheit)")
    # Validate the ENTIRE dense intended path against the safe box (not
    # just the coarse waypoints — a circle's extremes sit BETWEEN them).
    xs = [p[0] for p in intended_dense]
    zs = [p[1] for p in intended_dense]
    if (min(xs) < SAFE["x_min"] or max(xs) > SAFE["x_max"]
            or min(zs) < SAFE["z_min"] or max(zs) > SAFE["z_max"]):
        raise RoutePlanningError(
            "Bahn verlässt die sichere Box (x: 30..260, z: 35..150): "
            f"x[{min(xs):.1f}..{max(xs):.1f}], z[{min(zs):.1f}..{max(zs):.1f}] "
            "— Geometrie verkleinern/versetzen"
        )
    # Keep line/polyline vertices and segment junctions; allocate spare slots
    # to the arc with the largest angular step.
    sweeps = [float(s.get("sweep_deg", 360)) for s in segments if s["kind"] == "arc"]
    counts = [max(1, math.ceil(sweep / 90)) for sweep in sweeps]
    fixed = 1 + sum(1 if s["kind"] == "line" else len(s.get("points", []))
                    for s in segments if s["kind"] != "arc")
    if fixed + sum(counts) > _MAX_SLOTS:
        raise RoutePlanningError(
            "Die Bahn benötigt mehr als 10 Profilslots — in mehrere Läufe "
            "teilen (start = letzter Punkt des vorigen Laufs)")
    while counts and fixed + sum(counts) < _MAX_SLOTS:
        i = max(range(len(counts)), key=lambda i: sweeps[i] / counts[i])
        counts[i] += 1
    wps = _build_nominal(segments, intended_rough[0], arc_steps=counts)
    if closed:
        wps[-1] = wps[0]
    m = len(wps) - int(closed)

    def corner(j: int) -> float:
        """Auto blending radius for corner j (0 on straights/open ends)."""
        if not closed and (j == 0 or j == m - 1):
            return 0.0
        jp = (j - 1) % m if closed else j - 1
        jn = (j + 1) % m if closed else j + 1
        ax, ay = wps[j]
        v1x, v1y = wps[jp][0] - ax, wps[jp][1] - ay
        v2x, v2y = wps[jn][0] - ax, wps[jn][1] - ay
        l1 = math.hypot(v1x, v1y)
        l2 = math.hypot(v2x, v2y)
        if l1 < 1e-9 or l2 < 1e-9:
            return 0.0
        cosd = max(-1.0, min(1.0, (v1x * v2x + v1y * v2y) / (l1 * l2)))
        delta = math.acos(-cosd)  # 0 = straight
        if delta < 0.12:
            return 0.0
        est_r = (min(l1, l2) / 2.0) / math.tan(delta / 2.0)
        return round(min(l1 / 2.0, l2 / 2.0, 0.35 * est_r), 2)

    smooth: set[int] = set()
    slot = 0
    arc_counts = iter(counts)
    for seg in segments:
        if seg["kind"] == "arc":
            count = next(arc_counts)
            smooth.update(range(slot + 1, slot + count))
            if len(segments) == 1 and closed:
                smooth.add(0)
            slot += count
        else:
            slot += 1 if seg["kind"] == "line" else len(seg.get("points", []))
    blends = [corner(j) if j in smooth else 0.0 for j in range(m)]
    if closed:
        blends.append(blends[0])  # closing slot = same corner as the first

    waypoints = [
        {
            "x_pos": round(wx, 3),
            "z_pos": round(wz, 3),
            "path_vel": float(vel),
            "path_acc": float(acc),
            "path_dec": float(acc),
            "blending_radius": blends[i],
            "x_halt": i == len(wps) - 1,
        }
        for i, (wx, wz) in enumerate(wps)
    ]
    return RoutePlan(nominal=intended_dense, waypoints=waypoints, length=length, closed=closed)


# ---------------------------------------------------------------- verification
def _dist_to_poly(px: float, pz: float, poly: list[tuple[float, float]]) -> float:
    if len(poly) == 1:
        return math.dist((px, pz), poly[0])
    best = float("inf")
    for k in range(len(poly) - 1):
        ax, ay = poly[k]
        bx, by = poly[k + 1]
        seg2 = (bx - ax) ** 2 + (by - ay) ** 2
        if seg2 < 1e-9:
            continue
        t = ((px - ax) * (bx - ax) + (pz - ay) * (by - ay)) / seg2
        t = max(0.0, min(1.0, t))
        dx = px - (ax + t * (bx - ax))
        dy = pz - (ay + t * (by - ay))
        d2 = dx * dx + dy * dy
        if d2 < best:
            best = d2
    return math.sqrt(best)


def _deviation_tolerance(plan: RoutePlan) -> float:
    return min(12.0, max(5.0, 0.03 * plan.length))


def verify_route(
    plan: RoutePlan,
    result: SimResult,
    *,
    tol_end: float | None = None,
    tol_dev: float | None = None,
) -> dict:
    """Generic invariants for ANY shape — identical checks for circle,
    arc, line, polygon.  Returns ``{ok, metrics, reasons}`` where reasons
    are short German strings carrying value AND location so the agent can
    re-plan surgically."""
    nominal = plan.nominal
    traj = result.trajectory
    reasons: list[str] = []

    # Locate the start of the on-path phase (exclude the approach from
    # home — it legitimately runs through/around the shape).
    i0 = result.route_start_index
    if i0 >= len(traj):
        return {
            "ok": False,
            "metrics": {},
            "reasons": ["Roboter hat die Bahn nie erreicht (Anfahrt fehlgeschlagen)"],
        }
    body = traj[i0:]

    metrics: dict = {}
    # 1) completed (no hang, no premature stop, no protection)
    completed = bool(result.done) and not result.events
    if not completed:
        if result.events:
            reasons.append(f"Sicherheitseintritt: {result.events[0][1]}")
        elif not result.done:
            reasons.append(f"Lauf nicht fertig (Timeout nach {result.duration:.1f} s)")

    # 2) clean
    clean = not result.error and not result.events
    if not clean and not result.events:
        reasons.append(f"Fehler aktiv (error_id={result.error_id})")

    # 3) path fidelity: trail -> nominal deviation
    devs = [(_dist_to_poly(x, z, nominal), x, z) for (_t, x, z) in body]
    max_dev = max(d for d, _x, _z in devs)
    mean_dev = sum(d for d, _x, _z in devs) / len(devs)
    wx, wz = max(devs, key=lambda t: t[0])[1:]
    # General following-precision band: ~3% of path length, floored at 5
    # units (steady-state pursuit offset), capped at 12.
    tol_dev = tol_dev if tol_dev is not None else _deviation_tolerance(plan)
    if max_dev > tol_dev:
        reasons.append(
            f"Pfadtreue überschritten: max. Abweichung {max_dev:.1f} "
            f"(Toleranz {tol_dev:.1f}) bei ({wx:.1f}, {wz:.1f})"
        )
    metrics["max_dev"] = round(max_dev, 2)
    metrics["mean_dev"] = round(mean_dev, 2)
    metrics["tol_dev"] = round(tol_dev, 2)

    # 4) coverage: nominal -> trail (missed sections)
    cov_max = 0.0
    cov_pt = nominal[0]
    driven = [(x, z) for _, x, z in body]
    for (nx, nz) in nominal:
        d = _dist_to_poly(nx, nz, driven)
        if d > cov_max:
            cov_max = d
            cov_pt = (nx, nz)
    if cov_max > tol_dev:
        reasons.append(
            f"Bahnabschnitt nicht befahren: max. Lücke {cov_max:.1f} bei "
            f"({cov_pt[0]:.1f}, {cov_pt[1]:.1f})"
        )
    metrics["coverage_gap"] = round(cov_max, 2)

    # A blended loop starts and ends on the same tangent point. The
    # nominal geometry is still the independent reference for shape quality.
    target = result.nominal[-1] if plan.closed and result.nominal else nominal[-1]
    end_err = math.dist(result.end, target)
    if tol_end is None:
        tol_end = 3.0
    closure_error = 0.0
    if plan.closed:
        closure_error = math.dist(result.end, body[0][1:])
        metrics["closure_error"] = round(closure_error, 2)
        if closure_error > tol_end:
            reasons.append(f"Bahn nicht geschlossen: Lücke {closure_error:.1f}")
    if end_err > tol_end:
        reasons.append(
            f"Endposition abweicht: ({result.end[0]:.1f}, {result.end[1]:.1f}) "
            f"vs. erwartet ({target[0]:.1f}, {target[1]:.1f}), Δ={end_err:.1f} "
            f"(Toleranz {tol_end:.1f})"
        )
    metrics["end_err"] = round(end_err, 2)

    # An approach starting at Home may enter the box once. Any exit
    # after entry, or an approach starting elsewhere outside it, fails.
    out = []
    entered = False
    home_approach = math.dist(traj[0][1:], SIM_HOME) < 0.5
    # The live stage rests at drawing start (30, 35) with jog tolerance
    # (e.g. z=34.88); Robot.draw_by_jog accepts a start within 1 unit of
    # the box, so the approach may start/run there too.
    def _near(x: float, z: float) -> bool:
        return (SAFE["x_min"] - 1.0 <= x <= SAFE["x_max"] + 1.0
                and SAFE["z_min"] - 1.0 <= z <= SAFE["z_max"] + 1.0)

    for i, (_t, x, z) in enumerate(traj):
        inside = _inside(x, z)
        entered |= inside
        if i < i0 and not entered and _near(x, z):
            continue
        if not inside and (entered or not home_approach or i >= i0):
            out.append((x, z))
    if out:
        ox, oz = out[0]
        reasons.append(f"Ausreißer außerhalb der Box bei ({ox:.1f}, {oz:.1f})")
    metrics["out_of_box"] = len(out)

    # 7) monotonic progress (catches spirals / oscillation).  Closed loops
    # wrap: the projected arc resets at the lap, so differences are taken
    # modulo the path length (shortest signed step).
    max_drop = 0.0
    drop_pt = (0.0, 0.0)
    prev_s = high_water = 0.0
    prev = body[0][1:]
    for (_t, x, z) in body:
        # A crossing is not a shortcut to a distant part of the route.
        window = 2 * math.dist(prev, (x, z)) + tol_dev
        s = _arc_of(x, z, nominal, prev_s, window, plan.length if plan.closed else None)
        high_water = max(high_water, s)
        if high_water - s > max_drop:
            max_drop = high_water - s
            drop_pt = (x, z)
        prev_s, prev = s, (x, z)
    if max_drop > 2.0:
        reasons.append(
            f"Zurückbewegung/Pendeln: max. Rücktritt {max_drop:.1f} "
            f"bei ({drop_pt[0]:.1f}, {drop_pt[1]:.1f})"
        )
    metrics["max_backtrack"] = round(max_drop, 2)

    metrics["duration"] = round(result.duration, 2)
    metrics["end"] = [round(result.end[0], 2), round(result.end[1], 2)]
    ok = (completed and clean and max_dev <= tol_dev and cov_max <= tol_dev
          and end_err <= tol_end and closure_error <= tol_end and not out and max_drop <= 2.0)
    return {"ok": ok, "metrics": metrics, "reasons": reasons}


def _arc_of(px: float, pz: float, poly: list[tuple[float, float]],
            previous: float, window: float, period: float | None) -> float:
    """Projected arc length of a point onto a polyline."""
    best_s, best_d2 = 0.0, float("inf")
    cum = 0.0
    for k in range(len(poly) - 1):
        ax, ay = poly[k]
        bx, by = poly[k + 1]
        seglen = math.hypot(bx - ax, by - ay)
        if seglen < 1e-9:
            continue
        t = ((px - ax) * (bx - ax) + (pz - ay) * (by - ay)) / (seglen * seglen)
        t = max(0.0, min(1.0, t))
        dx = px - (ax + t * (bx - ax))
        dy = pz - (ay + t * (by - ay))
        d2 = dx * dx + dy * dy
        s = cum + t * seglen
        if period:
            s += round((previous - s) / period) * period
        if abs(s - previous) <= window and d2 < best_d2:
            best_d2, best_s = d2, s
        cum += seglen
    return previous if not math.isfinite(best_d2) else best_s


# ------------------------------------------------------------- plan vs. actual
def interpolate(traj: list[tuple[float, float, float]], t: float) -> tuple[float, float]:
    """Linear interpolation of a time-stamped trajectory at time ``t``."""
    if not traj:
        return (0.0, 0.0)
    if t <= traj[0][0]:
        return (traj[0][1], traj[0][2])
    if t >= traj[-1][0]:
        return (traj[-1][1], traj[-1][2])
    lo, hi = 0, len(traj) - 1
    while lo < hi:
        mid = (lo + hi) // 2
        if traj[mid][0] < t:
            lo = mid + 1
        else:
            hi = mid
    t0, x0, z0 = traj[lo - 1]
    t1, x1, z1 = traj[lo]
    f = 0.0 if t1 == t0 else (t - t0) / (t1 - t0)
    return (x0 + f * (x1 - x0), z0 + f * (z1 - z0))


def compare_run(
    sim_result: SimResult,
    actual: list[tuple[float, float, float]],
) -> dict:
    """Measured 10 Hz samples vs. the dry-run simulation.

    Spatial deviation at matching times plus ``duration_ratio`` (actual /
    simulated) — the measurable calibration factor between sim and plant.
    """
    if not actual:
        return {"ok": False, "reason": "keine Ist-Samples verfügbar"}
    # Spatial, not time-matched: live jog drawing runs at a different pace
    # than the sim, so equal timestamps sit at different places on the path
    # (22.11.: phantom max_err 73 on a clean run). Distance of each measured
    # sample to the nearest point of the simulated trajectory polyline.
    poly = [(x, z) for (_t, x, z) in sim_result.trajectory]

    def _dist(px: float, pz: float) -> float:
        if len(poly) == 1:
            return math.dist((px, pz), poly[0])
        best = float("inf")
        for (ax, az), (bx, bz) in zip(poly, poly[1:]):
            dx, dz = bx - ax, bz - az
            L2 = dx * dx + dz * dz
            u = 0.0 if L2 < 1e-12 else max(0.0, min(1.0, ((px - ax) * dx + (pz - az) * dz) / L2))
            best = min(best, math.hypot(px - ax - u * dx, pz - az - u * dz))
        return best

    errs = [_dist(x, z) for (_t, x, z) in actual]
    dur_ratio = (
        actual[-1][0] / sim_result.duration
        if sim_result.duration > 1e-6 else None
    )
    return {
        "ok": True,
        "n": len(actual),
        "max_err": round(max(errs), 2),
        "mean_err": round(sum(errs) / len(errs), 2),
        "duration_ratio": round(dur_ratio, 3) if dur_ratio is not None else None,
    }


# ------------------------------------------------------------------- rendering
def _encode_png(width: int, height: int, buf: bytes) -> bytes:
    def chunk(tag: bytes, data: bytes) -> bytes:
        c = struct.pack(">I", len(data)) + tag + data
        c += struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF)
        return c

    raw = b"".join(
        b"\x00" + bytes(buf[y * width * 3:(y + 1) * width * 3])
        for y in range(height)
    )
    ihdr = struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)
    return (b"\x89PNG\r\n\x1a\n"
            + chunk(b"IHDR", ihdr)
            + chunk(b"IDAT", zlib.compress(raw, 6))
            + chunk(b"IEND", b""))


def render_route_view(
    plan: RoutePlan,
    result: SimResult,
    *,
    size: tuple[int, int] = (460, 230),
    background: bytes | None = None,
) -> bytes:
    """Render planned path vs. driven trail to PNG (stdlib encoder).

    Layout mirrors the canvas: x -> width, z -> height (inverted), the
    safe box fills the frame.  Gray = nominal intent, blue = driven
    trail, red dots = points exceeding the deviation tolerance, dark =
    start, green = end.

    ``background``: optional raw RGB pixels (``w*h*3`` bytes, row-major,
    top row = z_max) stretched over the safe box, e.g. an uploaded photo
    of the workpiece.  It is drawn faded so the strokes stay readable.
    """
    w, h = size
    if background is not None and len(background) == w * h * 3:
        # Fade towards white (60 % image) so the route colors stay visible.
        buf = bytearray(int(102 + 0.6 * v) for v in background)
    else:
        buf = bytearray(b"\xf8\xfa\xfc" * (w * h))

    def put(px: int, py: int, rgb: bytes, r: int = 1) -> None:
        for dy in range(-r, r + 1):
            for dx in range(-r, r + 1):
                X, Y = px + dx, py + dy
                if 0 <= X < w and 0 <= Y < h:
                    o = (Y * w + X) * 3
                    buf[o:o + 3] = rgb

    def to_screen(x: float, z: float) -> tuple[int, int]:
        sx = (x - SAFE["x_min"]) * w / (SAFE["x_max"] - SAFE["x_min"])
        sy = (SAFE["z_max"] - z) * h / (SAFE["z_max"] - SAFE["z_min"])
        return int(sx), int(sy)

    def stroke(pts: list[tuple[int, int]], rgb: bytes, r: int = 1) -> None:
        for i in range(len(pts) - 1):
            (x0, y0), (x1, y1) = pts[i], pts[i + 1]
            d = max(abs(x1 - x0), abs(y1 - y0))
            for k in range(d + 1):
                f = 0.0 if d == 0 else k / d
                put(int(x0 + f * (x1 - x0)), int(y0 + f * (y1 - y0)), rgb, r)

    # Safe box frame
    frame = [(0, 0), (w - 1, 0), (w - 1, h - 1), (0, h - 1), (0, 0)]
    stroke(frame, b"\x00\x8c\x46")
    # Nominal intent (gray)
    stroke([to_screen(x, z) for (x, z) in plan.nominal], b"\x82\x87\x91")
    # Driven trail (blue), thinned for speed
    traj = result.trajectory
    stride = max(1, len(traj) // 1500)
    stroke([to_screen(x, z) for (_t, x, z) in traj[::stride]], b"\x23\x5a\xc8")
    # Deviating points (red)
    tol = _deviation_tolerance(plan)
    for (_t, x, z) in traj[result.route_start_index::stride]:
        if _dist_to_poly(x, z, plan.nominal) > tol:
            put(*to_screen(x, z), b"\xdc\x32\x32", r=2)
    # Start / end markers
    put(*to_screen(*plan.nominal[0]), b"\x19\x19\x1e", r=2)
    put(*to_screen(*result.end), b"\x00\x96\x3c", r=2)
    return _encode_png(w, h, bytes(buf))


# 3×5 bitmap digits for tile numbers (stdlib only, no font files).
_DIGITS = {
    "0": "111101101101111", "1": "010110010010111", "2": "111001111100111",
    "3": "111001111001111", "4": "101101111001001", "5": "111100111001111",
    "6": "111100111101111", "7": "111001001001001", "8": "111101111101111",
    "9": "111101111001111",
}


def _feature_check_points(waypoints: list[tuple[float, float]], half: float,
                          max_tiles: int) -> list[dict]:
    """Waypoints plus samples along each straight leg (what is driven)."""
    pts: list[tuple[float, float]] = []
    for p in waypoints:
        if not pts or math.dist(pts[-1], p) > 0.01:
            pts.append(p)
    spacing = 25.0
    while True:
        tiles: list[dict] = []
        for i, p in enumerate(pts):
            tiles.append({"kind": f"Wegpunkt {i + 1}", "point": i + 1,
                          "x": p[0], "z": p[1]})
            if i + 1 < len(pts):
                q = pts[i + 1]
                length = math.dist(p, q)
                if length > 2 * half:
                    n = max(2, math.ceil(length / spacing))
                    for k in range(1, n):
                        f = k / n
                        tiles.append({"kind": f"Strecke {i + 1}→{i + 2}",
                                      "leg": i + 1, "t": round(f, 2),
                                      "x": p[0] + f * (q[0] - p[0]),
                                      "z": p[1] + f * (q[1] - p[1])})
        if len(tiles) <= max_tiles or spacing > 400:
            return tiles[:max_tiles]
        spacing *= 1.5


def render_feature_check(
    plan: RoutePlan,
    background: bytes,
    bg_size: tuple[int, int],
    *,
    half: float = 8.0,
    tile_px: int = 192,
    cols: int = 6,
    max_tiles: int = 24,
) -> tuple[bytes, list[dict]]:
    """Planned route drawn SHARP on the background image for visual checks.

    Top: overview (920×460, full-strength image, route red, numbered
    markers). Below: zoomed tiles centred on every waypoint and on samples
    along each straight leg (the plant drives straight lines between
    waypoints): window ±``half`` units, fine grid every 1 unit (bright
    every 5), the driven line thin cyan, the tile centre marked by a ring.
    A feature exactly under the line/ring is "on the feature".

    ``background``: raw RGB ``bg_size`` pixels stretched over the safe box.
    Returns ``(png_bytes, tiles)``; tiles carry number, kind and centre.
    """
    bw, bh = bg_size
    if len(background) != bw * bh * 3:
        raise ValueError("background size mismatch")
    x0, x1 = SAFE["x_min"], SAFE["x_max"]
    z0, z1 = SAFE["z_min"], SAFE["z_max"]
    ow, oh = 920, 460
    tiles = _feature_check_points(
        [(w["x_pos"], w["z_pos"]) for w in plan.waypoints], half, max_tiles)
    for n, t in enumerate(tiles, 1):
        t["n"] = n
    rows = max(1, math.ceil(len(tiles) / cols))
    ML, MB = 36, 22  # ruler margins: z labels left, x labels below
    cell_w, cell_h = ML + tile_px + 8, tile_px + MB + 8
    tiles_y = oh + MB + 10
    W = max(ML + ow + 4, cols * cell_w)
    H = tiles_y + rows * cell_h
    buf = bytearray(b"\x28\x2a\x30" * (W * H))

    def sample(x: float, z: float) -> bytes:
        if not (x0 <= x <= x1 and z0 <= z <= z1):
            return b"\x50\x50\x50"
        px = min(bw - 1, int((x - x0) * bw / (x1 - x0)))
        py = min(bh - 1, int((z1 - z) * bh / (z1 - z0)))
        o = (py * bw + px) * 3
        return bytes(background[o:o + 3])

    def put(X: int, Y: int, rgb: bytes, alpha: float = 1.0) -> None:
        if 0 <= X < W and 0 <= Y < H:
            o = (Y * W + X) * 3
            if alpha >= 1.0:
                buf[o:o + 3] = rgb
            else:
                for c in range(3):
                    buf[o + c] = int(buf[o + c] * (1 - alpha) + rgb[c] * alpha)

    def line(ax: float, ay: float, bx: float, by: float, rgb: bytes,
             clip: tuple[int, int, int, int], r: int = 0) -> None:
        cx0, cy0, cx1, cy1 = clip
        d = max(1, int(max(abs(bx - ax), abs(by - ay)) * 2))
        for k in range(d + 1):
            f = k / d
            X, Y = int(ax + f * (bx - ax)), int(ay + f * (by - ay))
            for dy in range(-r, r + 1):
                for dx in range(-r, r + 1):
                    if cx0 <= X + dx < cx1 and cy0 <= Y + dy < cy1:
                        put(X + dx, Y + dy, rgb)

    def ring(cx: int, cy: int, radius: int, rgb: bytes) -> None:
        for k in range(64):
            a = 2 * math.pi * k / 64
            put(int(round(cx + radius * math.cos(a))),
                int(round(cy + radius * math.sin(a))), rgb)

    def number(text: str, X: int, Y: int, scale: int) -> None:
        wpx = len(text) * 4 * scale + scale
        for yy in range(Y, Y + 7 * scale):
            for xx in range(X, X + wpx):
                put(xx, yy, b"\xff\xff\xff")
        for i, ch in enumerate(text):
            bits = _DIGITS.get(ch)
            if not bits:
                continue
            for r_ in range(5):
                for c_ in range(3):
                    if bits[r_ * 3 + c_] == "1":
                        for sy in range(scale):
                            for sx in range(scale):
                                put(X + scale + (i * 4 + c_) * scale + sx,
                                    Y + scale + r_ * scale + sy, b"\x10\x10\x10")

    def rulers(left: int, top: int, w_px: int, h_px: int, xa: float, za: float,
               ppu_: float, minor: int, major: int, label: int) -> None:
        """Tick rulers in world units: x below the area, z left of it."""
        tick = b"\xd0\xd4\xdc"
        for u in range(math.ceil(xa), math.floor(xa + w_px / ppu_) + 1):
            if u % minor:
                continue
            X = left + int(round((u - xa) * ppu_))
            length = 6 if u % major == 0 else 3
            for k in range(length):
                put(X, top + h_px + 1 + k, tick)
            if u % label == 0:
                text = str(u)
                tw = len(text) * 8 + 2
                number(text, max(left, min(left + w_px - tw, X - tw // 2)),
                       top + h_px + 7, 2)
        for u in range(math.ceil(za - h_px / ppu_), math.floor(za) + 1):
            if u % minor:
                continue
            Y = top + int(round((za - u) * ppu_))
            length = 6 if u % major == 0 else 3
            for k in range(length):
                put(left - 2 - k, Y, tick)
            if u % label == 0:
                text = str(u)
                tw = len(text) * 8 + 2
                number(text, left - 8 - tw,
                       max(top, min(top + h_px - 14, Y - 7)), 2)

    # ---- overview: full-strength image, route red, numbered markers
    for Y in range(oh):
        z = z1 - (Y + 0.5) * (z1 - z0) / oh
        for X in range(ow):
            o = (Y * W + ML + X) * 3
            buf[o:o + 3] = sample(x0 + (X + 0.5) * (x1 - x0) / ow, z)
    rulers(ML, 0, ow, oh, x0, z1, ow / (x1 - x0), 5, 10, 10)

    def ov(x: float, z: float) -> tuple[float, float]:
        return ML + (x - x0) * ow / (x1 - x0), (z1 - z) * oh / (z1 - z0)

    wps = [(w["x_pos"], w["z_pos"]) for w in plan.waypoints]
    for a, b in zip(wps, wps[1:]):
        line(*ov(*a), *ov(*b), b"\xff\x20\x20", (ML, 0, ML + ow, oh), r=0)
    for t in tiles:
        X, Y = ov(t["x"], t["z"])
        ring(int(X), int(Y), 4, b"\xff\xe0\x00")
        number(str(t["n"]), int(X) + 6, int(Y) - 16, 2)

    # ---- zoom tiles
    ppu = tile_px / (2 * half)  # pixels per unit
    for i, t in enumerate(tiles):
        TX = (i % cols) * cell_w + ML
        TY = tiles_y + (i // cols) * cell_h
        cx, cz = t["x"], t["z"]
        for yy in range(tile_px):
            z = cz + half - (yy + 0.5) / ppu
            for xx in range(tile_px):
                o = ((TY + yy) * W + TX + xx) * 3
                buf[o:o + 3] = sample(cx - half + (xx + 0.5) / ppu, z)
        # world-aligned grid: every unit faint, every 5 units brighter
        for u in range(math.floor(cx - half), math.ceil(cx + half) + 1):
            xx = int(round((u - (cx - half)) * ppu))
            if 0 <= xx < tile_px:
                a = 0.55 if u % 5 == 0 else 0.18
                for yy in range(tile_px):
                    put(TX + xx, TY + yy, b"\xff\xff\xff", a)
        for u in range(math.floor(cz - half), math.ceil(cz + half) + 1):
            yy = int(round((cz + half - u) * ppu))
            if 0 <= yy < tile_px:
                a = 0.55 if u % 5 == 0 else 0.18
                for xx in range(tile_px):
                    put(TX + xx, TY + yy, b"\xff\xff\xff", a)
        clip = (TX, TY, TX + tile_px, TY + tile_px)

        def tp(x: float, z: float) -> tuple[float, float]:
            return TX + (x - (cx - half)) * ppu, TY + (cz + half - z) * ppu

        for a, b in zip(wps, wps[1:]):
            line(*tp(*a), *tp(*b), b"\x00\xe5\xff", clip)
        ring(TX + tile_px // 2, TY + tile_px // 2, 6, b"\xff\xe0\x00")
        number(str(t["n"]), TX + 2, TY + 2, 3)
        rulers(TX, TY, tile_px, tile_px, cx - half, cz + half, ppu, 1, 5, 5)
        t["x"], t["z"] = round(cx, 2), round(cz, 2)
    return _encode_png(W, H, bytes(buf)), tiles
