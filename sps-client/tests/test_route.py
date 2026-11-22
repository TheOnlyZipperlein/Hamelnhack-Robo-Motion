"""General route-planning + verification battery (plain python, no framework).

Runs the GENERIC invariants from sps_client.route.verify_route over a set of
shape classes (line, polygon, circle, arc, factory arc) plus a deliberate
negative case (safe-box violation must raise RoutePlanningError) and a
headless dry-run smoke test.  Execute:

    python -B tests/test_route.py

Exits 0 when all cases behave as expected.
"""

from __future__ import annotations

import math
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from sps_client.route import (  # noqa: E402
    RoutePlanningError,
    compare_run,
    interpolate,
    plan_route,
    render_feature_check,
    render_route_view,
    verify_route,
)
from sps_client.simulator import simulate_path  # noqa: E402

PASS = 0
FAIL = 0


def check(name: str, cond: bool, detail: str = "") -> None:
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  PASS  {name}")
    else:
        FAIL += 1
        print(f"  FAIL  {name}  {detail}")


def run_case(label: str, segments: list[dict], closed: bool = False,
             start: tuple[float, float] | None = None) -> dict:
    plan = plan_route(segments, start=start, closed=closed)
    result = simulate_path(plan.waypoints, start=start)
    verdict = verify_route(plan, result)
    print(f"[{label}] length={plan.length:.1f} slots={len(plan.waypoints)} "
          f"T={result.duration:.1f}s wall={result.wall_time * 1000:.0f}ms")
    if not verdict["ok"]:
        for r in verdict["reasons"]:
            print(f"        reason: {r}")
    return verdict


def main() -> int:
    # ---- 1) Straight line -------------------------------------------------
    v = run_case("LINE", [
        {"kind": "line", "to": {"x": 100, "z": 92}},
        {"kind": "line", "to": {"x": 200, "z": 92}},
    ])
    check("line: ok", v["ok"], str(v["reasons"]))
    check("line: end near (200,92)",
          math.hypot(v["metrics"]["end"][0] - 200, v["metrics"]["end"][1] - 92) < 2.5,
          str(v["metrics"]["end"]))
    check("line: low deviation", v["metrics"]["max_dev"] <= 5.0,
          str(v["metrics"]["max_dev"]))

    # ---- 2) Closed square polygon -----------------------------------------
    v = run_case("POLYGON", [
        {"kind": "line", "to": {"x": 120, "z": 60}},
        {"kind": "line", "to": {"x": 190, "z": 60}},
        {"kind": "line", "to": {"x": 190, "z": 120}},
        {"kind": "line", "to": {"x": 120, "z": 120}},
        {"kind": "line", "to": {"x": 120, "z": 60}},
    ])
    check("polygon: ok", v["ok"], str(v["reasons"]))
    check("polygon: closed detected",
          v["metrics"]["end_err"] < 6.0, str(v["metrics"]["end_err"]))

    # ---- 3) Circle r=50 (the quality case) --------------------------------
    cx, cz, r = 145.0, 92.0, 50.0
    v = run_case("CIRCLE r50", [
        {"kind": "arc", "center": {"x": cx, "z": cz}, "sweep_deg": 360},
    ], start=(cx + r, cz))
    check("circle: ok", v["ok"], str(v["reasons"]))
    check("circle: deviation from requested geometry <= 5", v["metrics"]["max_dev"] <= 5.0,
          str(v["metrics"]["max_dev"]))
    check("circle: closure gap <= 0.5", v["metrics"]["closure_error"] <= 0.5,
          str(v["metrics"]["closure_error"]))

    # ---- 4) Circle r=30 after approach legs (former 'egg' case) -----------
    v = run_case("CIRCLE r30 + approach", [
        {"kind": "line", "to": {"x": 145, "z": 92.5}},
        {"kind": "line", "to": {"x": 215, "z": 92.5}},
        {"kind": "arc", "center": {"x": 185, "z": 92.5}, "sweep_deg": 360},
    ])
    check("egg-case: ok", v["ok"], str(v["reasons"]))

    # ---- 5) Factory arc (open, regression) --------------------------------
    v = run_case("FACTORY ARC", [
        {"kind": "line", "to": {"x": 125, "z": 80}},
        {"kind": "line", "to": {"x": 90, "z": 115}},
    ])
    check("factory arc: ok", v["ok"], str(v["reasons"]))

    # ---- 6) Negative: safe-box violation must RAISE -----------------------
    raised = False
    try:
        plan_route([
            {"kind": "arc", "center": {"x": 145, "z": 92}, "sweep_deg": 360},
        ], start=(145 + 150, 92))  # r=150 >> box
    except RoutePlanningError as exc:
        raised = True
        msg = str(exc)
    check("box-violation raises RoutePlanningError", raised)
    if raised:
        check("box-violation message is actionable", "Box" in msg, msg)

    # ---- 7) Negative: slot budget ------------------------------------------
    raised = False
    try:
        # Very long zigzag -> more than 10 chords of 35 units won't happen,
        # so instead craft a long straight (length > 10*35 = 350 is impossible
        # in the box); use a small chord target indirectly: long straight
        # line from x=31 to x=259 = 228 units -> ceil(228/35)+1 = 8 slots ok.
        # Force overflow by a huge detour is not expressible; test the
        # degenerate empty route instead.
        plan_route([])
    except RoutePlanningError:
        raised = True
    check("empty segments raise", raised)

    # ---- 8) Headless dry-run smoke: protection is detectable ---------------
    # Drive INTO a raw limit: start near x-max, target BEYOND the 308 limit.
    res = simulate_path(
        [{"x_pos": 315.0, "z_pos": 92.0, "path_vel": 350.0, "path_acc": 1000.0,
          "path_dec": 1000.0, "blending_radius": 0.0, "x_halt": True}],
        start=(295.0, 92.0),
    )
    check("dry-run detects protection (21005)",
          res.error and res.error_id == 21005,
          f"error={res.error} id={res.error_id} events={res.events}")

    # ---- 9) Interpolation + compare_run ------------------------------------
    traj = [(0.0, 0.0, 0.0), (1.0, 10.0, 0.0), (2.0, 20.0, 0.0)]
    px, pz = interpolate(traj, 0.5)
    check("interpolate midpoint", abs(px - 5.0) < 1e-9 and pz == 0.0,
          f"({px},{pz})")
    fake_res = simulate_path(
        [{"x_pos": 100.0, "z_pos": 92.0, "path_vel": 350.0, "path_acc": 1000.0,
          "path_dec": 1000.0, "blending_radius": 0.0, "x_halt": True}],
        start=(15.0, -10.0),
    )
    cmp = compare_run(fake_res, [(t, x, z) for t, x, z in fake_res.trajectory[::10]])
    check("compare_run returns sane metrics",
          cmp["ok"] and cmp["max_err"] < 2.0 and (cmp["duration_ratio"] or 0) > 0,
          str(cmp))

    # ---- 10) Rendering produces a valid PNG --------------------------------
    plan = plan_route([{"kind": "arc", "center": {"x": 145, "z": 92}, "sweep_deg": 360}],
                      start=(195.0, 92.0))
    res = simulate_path(plan.waypoints, start=(195.0, 92.0))
    wrong = simulate_path([plan.waypoints[0]], start=(195.0, 92.0))
    check("verifier rejects a completed run that skipped the loop",
          wrong.done and not verify_route(plan, wrong)["ok"])
    png = render_route_view(plan, res)
    check("render: PNG magic", png[:8] == b"\x89PNG\r\n\x1a\n", repr(png[:8]))
    check("render: non-trivial size", 1000 < len(png) < 200_000, str(len(png)))
    bg = bytes([200, 40, 40]) * (460 * 230)
    png_bg = render_route_view(plan, res, background=bg)
    check("render: background changes the image", png_bg[:8] == png[:8] and png_bg != png)
    check("render: wrong-size background ignored",
          render_route_view(plan, res, background=b"\x00" * 10) == png)

    # ---- 11) Feature check: one image, tiles at waypoints + along legs ----
    seam = plan_route([{"kind": "polyline", "points": [
        {"x": 32, "z": 40}, {"x": 140, "z": 92}, {"x": 255, "z": 145}]}])
    hi = bytes([180, 180, 180]) * (920 * 460)
    fpng, tiles = render_feature_check(seam, hi, (920, 460))
    check("feature check: PNG magic", fpng[:8] == b"\x89PNG\r\n\x1a\n")
    kinds = [t["kind"] for t in tiles]
    check("feature check: every waypoint gets a tile",
          sum(k.startswith("Wegpunkt") for k in kinds) == 3, str(kinds))
    check("feature check: long legs sampled in between",
          sum(k.startswith("Strecke") for k in kinds) >= 8, str(kinds))
    check("feature check: tiles numbered 1..n",
          [t["n"] for t in tiles] == list(range(1, len(tiles) + 1)))
    check("feature check: low-res background accepted",
          render_feature_check(seam, bytes(460 * 230 * 3), (460, 230))[0][:4] == b"\x89PNG")

    print(f"\n{PASS} passed, {FAIL} failed")
    return 1 if FAIL else 0


if __name__ == "__main__":
    raise SystemExit(main())
