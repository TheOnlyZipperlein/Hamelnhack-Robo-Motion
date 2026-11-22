"""In-process robot simulator with the SAME public API as :class:`Robot`.

Purpose: let the web interface (and the pi agent) exercise the full
control stack without the plant.  ``SimRobot`` is a drop-in replacement
for ``Robot`` — every method, argument, and return type matches
(:mod:`sps_client.robot`), so ``SyncPlc`` and the Flask routes work
unchanged.  Selection happens in the web app via ``ROBOT_BACKEND=sim``.

Physical model (calibrated against live measurements, 21.11.2026 —
see ``AGENTS.md``):

* Coordinates are the plant's engineer units (home = (15, -10)).
* Jog: level signals, ~10 units/s, instant stop on flag clear.
* Homing: edge-triggered, latching flag, travels to (15, -10), sets
  ``g_xDone``; flag must be cleared manually afterwards (like the plant).
* Path runs: trapezoidal profile following the ``g_ascProfilePar``
  waypoints (velocity/acceleration taken from the profile entries).
* Protection: touching a RAW machine limit freezes all motion, sets
  ``g_xError`` + ``g_wErrorID=21005`` and engages the brake; the fault
  clears itself after ~8 s (observed plant behaviour).  Absolute limits:
  x: [-4.6, 308], z: [-12, 183.3].  The workspace guard z >= 14.5 only
  trips when crossed DOWWARDS from inside the workspace (except during
  homing) — home (15, -10) sits BELOW it in its own special corridor,
  which is exactly how the plant behaves (homing travels through the
  guard without fault; jogging z- from the workspace faults at 14.5).
* The SAFE box (x: [30, 260], z: [35, 150]) is NOT enforced here — it
  belongs to the application layer (simulator UI / agent rules), exactly
  like on the plant.

Simplifications vs. the plant (documented on purpose):

* No curved envelope (the plant's usable Z span narrows towards the
  edges); the simulator uses the full rectangular raw span.
* After a protection event self-clears, the robot is recovered to the
  centre of the safe box (the plant drifts it to a centre safe position)
  so it never rests exactly on a protection limit.
"""

from __future__ import annotations

import asyncio
import math
import time
from dataclasses import dataclass

from . import nodes
from .config import PlcConfig
from .robot import (
    Positions,
    ProfileParams,
    RobotStatus,
    WaitTimeout,
)

__all__ = [
    "SimRobot",
    "SimResult",
    "simulate_path",
    "SIM_SAFE_BOX",
    "SIM_RAW_LIMITS",
    "SIM_HOME",
]

#: Safe operating box recommended for applications (5 %+ margin).
SIM_SAFE_BOX = {"x_min": 30.0, "x_max": 260.0, "z_min": 35.0, "z_max": 150.0}

#: Raw machine limits (touching them triggers the 21005 protection).
SIM_RAW_LIMITS = {"x_min": -4.6, "x_max": 308.0, "z_min": 14.5, "z_max": 183.3}

#: Absolute floor below home (home corridor is unmapped; conservative
#: assumption: 2 units below the home point).
SIM_Z_FLOOR = -12.0

#: Machine home point (actuator coordinates, NOT (0, 0)).
SIM_HOME = (15.0, -10.0)

_JOG_SPEED = 10.0          # units/s (measured)
_HOMING_SPEED = 30.0       # units/s (observed magnitude)
_TICK_HZ = 60.0
_PROTECT_FREEZE_S = 8.0    # observed self-clear delay
_ERROR_ID_EDGE = 21005
_TRAIL_MAX = 4000
#: Steer lookahead for scheduled path tracking: base length on straights
#: and the fraction of the local curve radius in tight corners (a long
#: fixed lead cuts corners / swings wide -> "egg"; a very short lead
#: saturates the steering).  Tuned by offline ring-roundness sweeps.
_STEER_LEAD_BASE = 15.0
_STEER_LEAD_FRAC = 0.3


#: Jog-drawing model (mirror of ``Robot.draw_by_jog``, live 22.11.2026):
#: the plant draws ONLY axis-parallel moves (pure X or pure Z); diagonals
#: and arcs become a staircase of <= 2-unit steps, X first.
_JOG_ACC = 100.0           # units/s^2 (2-unit step ~0.3 s live)
_JOG_TOL = 0.4             # leg skipped when already within this distance
_JOG_STEP = 2.0            # staircase step (units)
_JOG_SETTLE_S = 0.15       # rest check at waypoints (live ~0.1-0.25 s)


def jog_draw_legs(
    cur: tuple[float, float],
    points: list[tuple[float, float]],
    *,
    tol: float = _JOG_TOL,
    step: float = _JOG_STEP,
    z_min: float = SIM_SAFE_BOX["z_min"],
) -> tuple[list[tuple[int, float, bool]], int]:
    """Axis-parallel legs ``(axis, target, settle)`` exactly as the live
    ``Robot.draw_by_jog`` drives them, plus the number of approach legs.

    Approach: L-shape to ``points[0]`` (Z first when below the box). Then
    per point: axis-parallel -> one leg per axis; otherwise a staircase of
    ``n = ceil(max(|dx|, |dz|) / step)`` X/Z step pairs; every leg ends
    with a rest check, like live.
    """
    legs: list[tuple[int, float, bool]] = []
    pos = [float(cur[0]), float(cur[1])]

    def leg(axis: int, target: float, settle: bool = True) -> None:
        if abs(target - pos[axis]) > tol:
            legs.append((axis, float(target), settle))
            pos[axis] = float(target)

    first = points[0]
    for axis in ((1, 0) if pos[1] < z_min else (0, 1)):
        leg(axis, first[axis])
    approach = len(legs)
    for tx, tz in points[1:]:
        dx, dz = tx - pos[0], tz - pos[1]
        if abs(dx) <= tol or abs(dz) <= tol:
            leg(0, tx)
            leg(1, tz)
            continue
        n = max(1, math.ceil(max(abs(dx), abs(dz)) / step))
        sx, sz = pos
        for k in range(1, n + 1):
            leg(0, sx + dx * k / n)
            leg(1, sz + dz * k / n)
    return legs, approach


def _bezier_points(t1: tuple[float, float], cv: tuple[float, float],
                   t2: tuple[float, float], steps: int = 6) -> list[tuple[float, float]]:
    """Sample a quadratic Bézier (control point ``cv``) from ``t1`` to
    ``t2``, EXCLUDING the start point."""
    pts: list[tuple[float, float]] = []
    for k in range(1, steps + 1):
        t = k / steps
        w1, w2, w3 = (1 - t) ** 2, 2 * (1 - t) * t, t ** 2
        pts.append((w1 * t1[0] + w2 * cv[0] + w3 * t2[0],
                    w1 * t1[1] + w2 * cv[1] + w3 * t2[1]))
    return pts


def _expand_blend(sel: list[dict]) -> list[tuple[float, float, float, float]]:
    """Expand selected profile slots into a motion queue, rounding each
    vertex according to its ``blending_radius`` (as the plant does — the
    factory curve ships with BlendingRadius=2). Without it a 10-point
    circle looks like a polygon; with it, nearly round.

    Each vertex is replaced by a quadratic Bézier (vertex = control point)
    between tangent points on the incoming/outgoing edges, clamped to half
    the neighbouring edge lengths. If the list is CLOSED (first ≈ last
    point), the loop is expanded cyclically so the closing vertex is
    rounded too; the run starts and ends at the outgoing tangent point.
    """
    pts = [
        (float(p["x_pos"]), float(p["z_pos"]),
         float(p["path_vel"]), float(p["path_acc"]),
         float(p["blending_radius"]))
        for p in sel
    ]
    n = len(pts)
    if n < 3:
        return [(x, z, v, a) for (x, z, v, a, _r) in pts]

    def tangents(i: int, prev_i: int, next_i: int):
        vx, vz = pts[i][0], pts[i][1]
        px, pz = pts[prev_i][0], pts[prev_i][1]
        nx_, nz_ = pts[next_i][0], pts[next_i][1]
        l1 = math.hypot(vx - px, vz - pz)
        l2 = math.hypot(nx_ - vx, nz_ - vz)
        r = min(pts[i][4], l1 / 2.0, l2 / 2.0)
        if r < 0.05 or l1 < 1e-9 or l2 < 1e-9:
            return None
        u1x, u1y = (vx - px) / l1, (vz - pz) / l1
        u2x, u2y = (nx_ - vx) / l2, (nz_ - vz) / l2
        return (
            (vx - u1x * r, vz - u1y * r),
            (vx + u2x * r, vz + u2y * r),
        )

    closed = n >= 4 and math.hypot(
        pts[0][0] - pts[-1][0], pts[0][1] - pts[-1][1]) < 0.05

    if not closed:
        out: list[tuple[float, float, float, float]] = [
            (pts[0][0], pts[0][1], pts[0][2], pts[0][3])
        ]
        for i in range(1, n - 1):
            vx, vz, vel, acc = pts[i][:4]
            tg = tangents(i, i - 1, i + 1)
            if tg is None:
                out.append((vx, vz, vel, acc))
                continue
            t1, t2 = tg
            out.append((t1[0], t1[1], vel, acc))
            out.extend((bx, bz, vel, acc)
                       for bx, bz in _bezier_points(t1, (vx, vz), t2))
        out.append((pts[-1][0], pts[-1][1], pts[-1][2], pts[-1][3]))
        return out

    # Closed loop: distinct corners C0..C_{m-1}; the edge after C_{m-1}
    # wraps around to C0 (P_{n-1} coincides with P0).
    m = n - 1
    tg0 = tangents(0, m - 1, 1)
    if tg0 is None:
        # Sharp closing corner: the loop must STILL close — run the full
        # circuit, starting and ending exactly at C0 (unblended).
        out = [(pts[0][0], pts[0][1], pts[0][2], pts[0][3])]
        for i in range(1, m):
            vx, vz, vel, acc = pts[i][:4]
            tg = tangents(i, i - 1, i + 1)
            if tg is None:
                out.append((vx, vz, vel, acc))
                continue
            t1, t2 = tg
            out.append((t1[0], t1[1], vel, acc))
            out.extend((bx, bz, vel, acc)
                       for bx, bz in _bezier_points(t1, (vx, vz), t2))
        out.append((pts[0][0], pts[0][1], pts[0][2], pts[0][3]))
        return out
    t1_0, t2_0 = tg0
    vel0, acc0 = pts[0][2], pts[0][3]
    out = [(t2_0[0], t2_0[1], vel0, acc0)]
    for i in range(1, m):
        vx, vz, vel, acc = pts[i][:4]
        tg = tangents(i, i - 1, i + 1)
        if tg is None:
            out.append((vx, vz, vel, acc))
            continue
        t1, t2 = tg
        out.append((t1[0], t1[1], vel, acc))
        out.extend((bx, bz, vel, acc)
                   for bx, bz in _bezier_points(t1, (vx, vz), t2))
    # Round the closing corner C0 and finish at its outgoing tangent point.
    out.append((t1_0[0], t1_0[1], vel0, acc0))
    out.extend((bx, bz, vel0, acc0)
               for bx, bz in _bezier_points(t1_0, (pts[0][0], pts[0][1]), t2_0))
    return out


class SimRobot:
    """Drop-in simulation backend implementing the :class:`Robot` API.

    Example (mirrors the live usage)::

        async with SimRobot() as bot:
            await bot.home()
            await bot.wait_done(30)
            await bot.start_path(1)
            await bot.wait_done(60)
    """

    # ------------------------------------------------------------------ life
    def __init__(self, config: PlcConfig | None = None) -> None:
        self.config = config if config is not None else PlcConfig(dry_run=True)
        self._task: asyncio.Task | None = None
        self._connected = False
        self._seed()

    def _seed(self) -> None:
        # Kinematic state
        self._x, self._z = SIM_HOME
        self._vx = self._vz = 0.0
        # Internal simulation clock (NOT wall time): keeps _step free of
        # real-time dependence so headless dry runs can fast-forward and
        # protection timing stays consistent under time_scale.
        self._clock = 0.0
        # Playback speed of the VISIBLE simulation (1.0 = plant time).
        self._time_scale = 1.0
        self._tick_budget = 0.0
        # Motion mode: "idle" | "jog" | "home" | "path"
        self._mode = "idle"
        self._path_queue: list[tuple[float, float, float, float]] = []
        # Command flags (mirror of the GVL booleans)
        self._flags: dict[str, bool] = {
            "done": False,
            "error": False,
            "warning": False,
            "ack_path_halt": False,
            "home_pos_available": False,
            "motor_brake_released": True,
            "gripper_ctrl_active": False,
            "path_execute": False,
            "path_stop": False,
            "path_interrupt": False,
            "home_execute": False,      # latching, like the plant
            "jog_x_pos": False,
            "jog_x_neg": False,
            "jog_z_pos": False,
            "jog_z_neg": False,
        }
        self._error_id = 0
        # Set values
        self._override = 1.0
        self._set_path_vel_out = 0.0
        # Protection state
        self._frozen_until: float | None = None
        # Profile bank: seeded with the plant's measured profile curve
        # (10 waypoints, arc from (125, 80) to (90, 115)).
        self._profiles: list[dict] = [
            {"x_pos": 125.0, "z_pos": 80.0, "path_vel": 350.0,
             "path_acc": 1000.0, "path_dec": 1000.0,
             "blending_radius": 0.0 if i == 0 else 2.0, "x_halt": False}
            for i in range(10)
        ]
        # Waypoints 2..10 follow the measured arc
        _arc = [(124.5, 86.1), (122.9, 92.0), (120.3, 97.5), (116.8, 102.5),
                (112.5, 106.8), (107.5, 110.3), (102.0, 112.9),
                (96.1, 114.5), (90.0, 115.0)]
        for i, (px, pz) in enumerate(_arc, start=1):
            self._profiles[i]["x_pos"] = px
            self._profiles[i]["z_pos"] = pz
        # Recording (for the visualisation trail)
        self._trail: list[tuple[float, float]] = [(self._x, self._z)]
        self._positioning = False
        self._positioning_abort = False
        # Path-following bookkeeping (rebuilt in start_path)
        self._path_cum: list[float] = []
        self._path_total = 0.0
        # Time-scheduled trajectory (planned ahead in start_path)
        self._sched_pts: list[tuple[float, float]] = []
        self._sched_arcs: list[float] = []
        self._sched_time: list[float] = []
        self._sched_speed: list[float] = []
        self._sched_kappa: list[float] = []
        self._run_T = 0.0
        self._t_elapsed = 0.0
        # Jog-drawing legs (axis, target, settle), see jog_draw_legs
        self._legs: list[tuple[int, float, bool]] = []
        self._leg_i = 0
        self._leg_approach = 0
        self._leg_dwell = 0.0

    async def connect(self) -> None:
        if self._connected:
            return
        self._connected = True
        self._task = asyncio.get_running_loop().create_task(
            self._tick_loop(), name="sim-robot-tick"
        )

    async def close(self) -> None:
        if not self._connected:
            return
        self._connected = False
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except (asyncio.CancelledError, Exception):  # noqa: BLE001
                pass
            self._task = None

    async def __aenter__(self) -> "SimRobot":
        await self.connect()
        return self

    async def __aexit__(self, *exc_info: object) -> None:
        await self.close()

    # ------------------------------------------------------- physics engine
    async def _tick_loop(self) -> None:
        # Advance by REAL elapsed time so the sim runs in real time even
        # when the event loop is slower than the nominal tick rate.
        last = time.monotonic()
        while True:
            await asyncio.sleep(1.0 / _TICK_HZ)
            now = time.monotonic()
            dt = now - last
            last = now
            if dt > 0:
                # time_scale speeds the visible sim up (2x, 5x, ...);
                # headless dry runs (simulate_path) don't tick at all.
                budget = min(dt, 0.1) * self._time_scale
                self._tick_budget += budget
                step = 1 / _TICK_HZ
                while self._tick_budget >= step:
                    self._step(step)
                    self._tick_budget -= step

    def _step(self, dt: float) -> None:
        self._clock += dt
        now = self._clock
        # Protection freeze: no motion at all, then self-clear.
        if self._frozen_until is not None:
            if now >= self._frozen_until:
                self._frozen_until = None
                self._flags["error"] = False
                self._flags["warning"] = False
                self._flags["motor_brake_released"] = True
                self._error_id = 0
                self._mode = "idle"
                self._vx = self._vz = 0.0
                # Recovery: relocate to the safe-box centre (plant-like),
                # so the robot never rests exactly on a protection limit
                # and the fault cannot immediately re-trigger.
                self._x = (SIM_SAFE_BOX["x_min"] + SIM_SAFE_BOX["x_max"]) / 2
                self._z = (SIM_SAFE_BOX["z_min"] + SIM_SAFE_BOX["z_max"]) / 2
                self._trail.append((self._x, self._z))
            else:
                return

        ax = az = 0.0
        if self._mode == "home":
            ax, az = self._follow(SIM_HOME[0], SIM_HOME[1], _HOMING_SPEED, 500.0, dt)
            if self._near(SIM_HOME[0], SIM_HOME[1], 0.05):
                self._mode = "idle"
                self._flags["done"] = True
                self._flags["home_pos_available"] = True
                ax = az = 0.0
                self._vx = self._vz = 0.0
        elif self._mode == "path" and self._path_queue:
            ax, az = self._step_jog_draw(dt)
        elif self._mode == "jog":
            if self._flags["jog_x_pos"]:
                ax = _JOG_SPEED
            elif self._flags["jog_x_neg"]:
                ax = -_JOG_SPEED
            if self._flags["jog_z_pos"]:
                az = _JOG_SPEED
            elif self._flags["jog_z_neg"]:
                az = -_JOG_SPEED
            if ax == 0.0 and az == 0.0:
                self._mode = "idle"
        # Integrate
        pz_prev = self._z
        self._vx, self._vz = ax, az
        self._x += ax * dt
        self._z += az * dt
        # Envelope: raw limits trigger the protection (like the plant).
        lim = SIM_RAW_LIMITS
        crossed_down = pz_prev >= lim["z_min"] > self._z
        fault = (
            self._x <= lim["x_min"]
            or self._x >= lim["x_max"]
            or self._z >= lim["z_max"]
            or self._z <= SIM_Z_FLOOR
            # Workspace guard: only when leaving the workspace downwards
            # (homing legitimately travels through it to the home corridor).
            or (crossed_down and self._mode != "home")
        )
        if fault:
            if self._x <= lim["x_min"]:
                self._x = lim["x_min"]
            elif self._x >= lim["x_max"]:
                self._x = lim["x_max"]
            if self._z <= SIM_Z_FLOOR:
                self._z = SIM_Z_FLOOR
            elif self._z >= lim["z_max"]:
                self._z = lim["z_max"]
            elif crossed_down and self._mode != "home":
                self._z = lim["z_min"]
            self._trigger_protection()
            return
        # Record trail
        lx, lz = self._trail[-1]
        if math.hypot(self._x - lx, self._z - lz) > 0.05:
            self._trail.append((self._x, self._z))
            if len(self._trail) > _TRAIL_MAX:
                del self._trail[: len(self._trail) - _TRAIL_MAX]

    def _step_jog_draw(self, dt: float) -> tuple[float, float]:
        """One tick of the jog-drawing model: a single axis at a time,
        trapezoidal at _JOG_SPEED/_JOG_ACC, then an optional rest dwell."""
        if self._t_elapsed >= 0.0:
            self._t_elapsed += dt
        if self._leg_dwell > 0.0:
            self._leg_dwell -= dt
            return 0.0, 0.0
        if self._leg_i >= len(self._legs):
            self._finish_path()
            return 0.0, 0.0
        axis, target, settle = self._legs[self._leg_i]
        pos = self._x if axis == 0 else self._z
        v = self._vx if axis == 0 else self._vz
        dist = target - pos
        sign = 1.0 if dist > 0 else -1.0
        v_allow = min(_JOG_SPEED, math.sqrt(2.0 * _JOG_ACC * abs(dist)))
        v_new = sign * min(v_allow, abs(v) + _JOG_ACC * dt) if v * sign >= 0 else 0.0
        if abs(dist) <= max(abs(v_new) * dt, 1e-4):
            v_new = dist / dt  # land exactly on the target this tick
            self._leg_i += 1
            if settle:
                self._leg_dwell = _JOG_SETTLE_S
            if self._leg_i == self._leg_approach:
                self._t_elapsed = 0.0  # on the path: drawing starts here
        return (v_new, 0.0) if axis == 0 else (0.0, v_new)

    def _follow(self, tx: float, tz: float, vmax: float, acc: float, dt: float,
                v_cap: float | None = None) -> tuple[float, float]:
        """Trapezoidal-profile follower. Returns the velocity vector.

        ``v_cap`` overrides the internal stop-distance cap — the path
        runner passes a cap computed over the WHOLE remaining path so the
        robot cruises through intermediate targets without micro-stopping.
        """
        dx, dz = tx - self._x, tz - self._z
        dist = math.hypot(dx, dz)
        if dist < 1e-9:
            return 0.0, 0.0
        if v_cap is not None:
            v_allow = v_cap
        else:
            # Velocity cap so we can always stop inside the remaining distance.
            v_allow = min(vmax, math.sqrt(2.0 * acc * dist))
        ux, uz = dx / dist, dz / dist
        return self._limit_velocity(ux * v_allow, uz * v_allow, acc, dt)

    def _limit_velocity(self, vx: float, vz: float, acc: float, dt: float) -> tuple[float, float]:
        dvx = vx - self._vx
        dvz = vz - self._vz
        dvmag = math.hypot(dvx, dvz)
        scale = 1.0 if dvmag <= acc * dt else (acc * dt) / dvmag
        return self._vx + dvx * scale, self._vz + dvz * scale

    def _near(self, tx: float, tz: float, eps: float) -> bool:
        return math.hypot(tx - self._x, tz - self._z) < eps

    def _finish_path(self) -> None:
        """Close out a path run cleanly (zero velocity, flags set)."""
        self._path_queue = []
        self._mode = "idle"
        self._flags["done"] = True
        self._flags["path_execute"] = False
        self._vx = self._vz = 0.0

    def _planned_state(self, t: float) -> tuple[float, float, float, float]:
        """(x, z, speed, arc) of the planned trajectory at sim time ``t``.

        Interpolated on the precomputed schedule; clamped to the final
        point once the run time is reached (the target then HOLDS at the
        finish until the robot arrives)."""
        st, sv = self._sched_time, self._sched_speed
        sa, sp = self._sched_arcs, self._sched_pts
        if t >= self._run_T:
            return sp[-1][0], sp[-1][1], 0.0, sa[-1]
        if t <= 0.0:
            return sp[0][0], sp[0][1], sv[0], sa[0]
        lo, hi = 0, len(st) - 1
        while lo < hi:  # largest i with st[i] <= t
            mid = (lo + hi + 1) // 2
            if st[mid] <= t:
                lo = mid
            else:
                hi = mid - 1
        if lo + 1 >= len(st):
            return sp[-1][0], sp[-1][1], 0.0, sa[-1]
        span = st[lo + 1] - st[lo]
        u = 0.0 if span < 1e-9 else (t - st[lo]) / span
        v0, v1 = sv[lo], sv[lo + 1]
        fr = u if v0 + v1 < 1e-3 else u * (2 * v0 + u * (v1 - v0)) / (v0 + v1)
        return (
            sp[lo][0] + fr * (sp[lo + 1][0] - sp[lo][0]),
            sp[lo][1] + fr * (sp[lo + 1][1] - sp[lo][1]),
            sv[lo] + fr * (sv[lo + 1] - sv[lo]),
            sa[lo] + fr * (sa[lo + 1] - sa[lo]),
        )

    def _planned_point_at_arc(self, s: float) -> tuple[float, float]:
        """Planned point at arc length ``s`` (clamped)."""
        sa, sp = self._sched_arcs, self._sched_pts
        if s <= 0.0:
            return sp[0][0], sp[0][1]
        if s >= sa[-1]:
            return sp[-1][0], sp[-1][1]
        lo, hi = 0, len(sa) - 1
        while lo < hi:  # largest i with sa[i] <= s
            mid = (lo + hi + 1) // 2
            if sa[mid] <= s:
                lo = mid
            else:
                hi = mid - 1
        span = sa[lo + 1] - sa[lo]
        fr = 0.0 if span < 1e-9 else (s - sa[lo]) / span
        return (
            sp[lo][0] + fr * (sp[lo + 1][0] - sp[lo][0]),
            sp[lo][1] + fr * (sp[lo + 1][1] - sp[lo][1]),
        )

    def _radius_at_arc(self, s: float) -> float:
        """Curve radius at arc length ``s`` (``inf`` on straight sections).

        Interpolates the PLANNED curvature (1/R), which is smooth, then
        inverts — used to shrink the steer lookahead in tight corners."""
        sa, sk = self._sched_arcs, self._sched_kappa
        if s <= 0.0:
            k = sk[0]
        elif s >= sa[-1]:
            k = sk[-1]
        else:
            lo, hi = 0, len(sa) - 1
            while lo < hi:  # largest i with sa[i] <= s
                mid = (lo + hi + 1) // 2
                if sa[mid] <= s:
                    lo = mid
                else:
                    hi = mid - 1
            span = sa[lo + 1] - sa[lo]
            fr = 0.0 if span < 1e-9 else (s - sa[lo]) / span
            k = sk[lo] + fr * (sk[lo + 1] - sk[lo])
        if k < 1e-9:
            return float("inf")
        return 1.0 / k

    def _plan_trajectory(self) -> None:
        """Precompute a time-scheduled trajectory for the queued path.

        1. Resample the polyline at ~2 unit spacing.
        2. Estimate the local curve radius at every sample (circumcircle
           of the neighbours) and limit speed to v = sqrt(k·a·R) so the
           lateral acceleration stays within budget (k = 0.85).
        3. Forward pass (accelerate) + backward pass (pre-brake) make the
           profile dynamically feasible; both ends rest at v = 0.
        4. Integrate the time schedule T(s).

        The step loop then only has to CHASE this plan (see ``_step``).
        """
        q = self._path_queue
        cum = self._path_cum
        total = self._path_total
        if total < 1e-9:
            self._sched_pts = [(q[0][0], q[0][1])]
            self._sched_arcs = [0.0]
            self._sched_time = [0.0]
            self._sched_speed = [0.0]
            self._sched_kappa = [0.0]
            self._run_T = 0.0
            return

        # 1) Resample
        ds_sample = 2.0
        pts: list[tuple[float, float]] = [(q[0][0], q[0][1])]
        ss: list[float] = [0.0]
        va: list[float] = [q[0][2]]
        ac: list[float] = [q[0][3]]
        for k in range(1, len(q)):
            seg = cum[k] - cum[k - 1]
            if seg < 1e-9:
                continue
            n = max(1, int(round(seg / ds_sample)))
            for j in range(1, n + 1):
                t = j / n
                s = cum[k - 1] + t * seg
                pts.append((
                    q[k - 1][0] + t * (q[k][0] - q[k - 1][0]),
                    q[k - 1][1] + t * (q[k][1] - q[k - 1][1]),
                ))
                ss.append(s)
                va.append(q[k][2])
                ac.append(q[k][3])
        m = len(pts)

        # 2) Curve radius per sample + speed limit
        vlim: list[float] = []
        kappa: list[float] = []  # curvature 1/R (0 = straight)
        for i in range(m):
            p0 = pts[max(i - 1, 0)]
            p1 = pts[i]
            p2 = pts[min(i + 1, m - 1)]
            a = math.hypot(p1[0] - p0[0], p1[1] - p0[1])
            b = math.hypot(p2[0] - p1[0], p2[1] - p1[1])
            c = math.hypot(p2[0] - p0[0], p2[1] - p0[1])
            cross = abs((p1[0] - p0[0]) * (p2[1] - p0[1])
                        - (p1[1] - p0[1]) * (p2[0] - p0[0]))
            if i not in (0, m - 1) and any(math.dist(p1, (x, z)) < 1e-8
                    for x, z, _, _ in q[1:-1]) and min(a, b) > 1e-9 and (
                        (p1[0] - p0[0]) * (p2[0] - p1[0])
                        + (p1[1] - p0[1]) * (p2[1] - p1[1]) < 0.9 * a * b):
                vlim.append(0.0)  # sharp corner: stop before changing direction
                kappa.append(0.0)
            elif min(a, b, c) < 1e-9 or cross < 1e-9:
                vlim.append(va[i])  # straight: profile speed
                kappa.append(0.0)
            else:
                r = (a * b * c) / (2.0 * cross)
                vlim.append(min(va[i], math.sqrt(0.85 * ac[i] * r)))
                kappa.append(1.0 / r)

        # 3) Feasible profile: accelerate forward, pre-brake backwards
        vf = [0.0] * m
        for i in range(1, m):
            ds = ss[i] - ss[i - 1]
            vf[i] = min(vlim[i],
                        math.sqrt(max(0.0, vf[i - 1] ** 2
                                      + 2.0 * ac[i] * ds)))
        vb = [0.0] * m
        for i in range(m - 2, -1, -1):
            ds = ss[i + 1] - ss[i]
            vb[i] = min(vlim[i],
                        math.sqrt(max(0.0, vb[i + 1] ** 2
                                      + 2.0 * ac[i + 1] * ds)))
        vs = [min(vf[i], vb[i]) for i in range(m)]

        # 4) Time schedule
        tt = [0.0] * m
        for i in range(m - 1):
            ds = ss[i + 1] - ss[i]
            vm = 0.5 * (vs[i] + vs[i + 1])
            tt[i + 1] = tt[i] + (ds / vm if vm > 1e-3 else ds / 5.0)

        self._sched_pts = pts
        self._sched_arcs = ss
        self._sched_time = tt
        self._sched_speed = vs
        self._sched_kappa = kappa
        self._run_T = tt[-1]

    def _trigger_protection(self) -> None:
        self._frozen_until = self._clock + _PROTECT_FREEZE_S
        self._flags["error"] = True
        self._flags["done"] = False
        self._flags["motor_brake_released"] = False
        self._error_id = _ERROR_ID_EDGE
        self._mode = "idle"
        self._vx = self._vz = 0.0

    # ------------------------------------------------------------ reads (API)
    async def status(self) -> RobotStatus:
        f = self._flags
        return RobotStatus(
            done=f["done"],
            error=f["error"],
            warning=f["warning"],
            ack_path_halt=f["ack_path_halt"],
            home_pos_available=f["home_pos_available"],
            motor_brake_released=f["motor_brake_released"],
            gripper_ctrl_active=f["gripper_ctrl_active"],
            error_id=self._error_id,
            stat_words=(0,) * 7,
            ctrl_words=(0,) * 8,
        )

    async def positions(self) -> Positions:
        return Positions(
            act_x_pos=self._round(self._x),
            act_x_vel=self._round(self._vx),
            act_z_pos=self._round(self._z),
            act_z_vel=self._round(self._vz),
            act_values=(0.0,) * 8,
            set_values=(0.0,) * 8,
            override=self._override,
            set_path_vel_out=self._set_path_vel_out,
        )

    @staticmethod
    def _round(v: float) -> float:
        # Mimic the plant's quantized act readings (~0.076 noise when idle
        # is intentionally NOT simulated; clean values keep the UI stable).
        return round(v, 3)

    async def get_profile(self, index: int) -> ProfileParams:
        self._validate_profile_index(index)
        p = self._profiles[index - 1]
        return ProfileParams(
            index=index,
            x_pos=p["x_pos"], z_pos=p["z_pos"], path_vel=p["path_vel"],
            path_acc=p["path_acc"], path_dec=p["path_dec"],
            blending_radius=p["blending_radius"], x_halt=p["x_halt"],
        )

    async def set_profile(
        self,
        index: int,
        *,
        x_pos: float | None = None,
        z_pos: float | None = None,
        path_vel: float | None = None,
        path_acc: float | None = None,
        path_dec: float | None = None,
        blending_radius: float | None = None,
        x_halt: bool | None = None,
    ) -> None:
        self._validate_profile_index(index)
        p = self._profiles[index - 1]
        if x_pos is not None:
            p["x_pos"] = float(x_pos)
        if z_pos is not None:
            p["z_pos"] = float(z_pos)
        if path_vel is not None:
            p["path_vel"] = float(path_vel)
        if path_acc is not None:
            p["path_acc"] = float(path_acc)
        if path_dec is not None:
            p["path_dec"] = float(path_dec)
        if blending_radius is not None:
            p["blending_radius"] = float(blending_radius)
        if x_halt is not None:
            p["x_halt"] = bool(x_halt)

    # -------------------------------------------------------- motion (API)
    async def start_path(
        self,
        profile_index: int | None = None,
        *,
        path_vel_out: float | None = None,
        override: float | None = None,
    ) -> None:
        if profile_index is not None:
            self._validate_profile_index(profile_index)
            sel = [self._profiles[profile_index - 1]]
        else:
            # Whole-bank run: execute from slot 1, stopping at the first
            # waypoint flagged x_halt=True (inclusive) — everything beyond
            # a halt mark is stale data from a previous path definition.
            sel = []
            for p in self._profiles:
                sel.append(p)
                if p["x_halt"]:
                    break
        if path_vel_out is not None:
            self._set_path_vel_out = float(path_vel_out)
        if override is not None:
            self._override = float(override)
        self._prepare_path_run(sel)

    def _prepare_path_run(self, sel: list[dict]) -> None:
        """Shared path-run setup (visible runs AND headless dry runs).

        Motion model = live jog drawing: only straight horizontal/vertical
        moves (see :func:`jog_draw_legs`); ``blending_radius`` and the
        profile speeds have no effect, exactly like on the plant.
        """
        queue = [(float(p["x_pos"]), float(p["z_pos"]),
                  float(p["path_vel"]), float(p["path_acc"])) for p in sel]
        self._legs, self._leg_approach = jog_draw_legs(
            (self._x, self._z), [(x, z) for x, z, _v, _a in queue])
        self._leg_i = 0
        self._leg_dwell = 0.0
        self._flags["path_execute"] = True
        self._flags["done"] = False
        self._flags["home_pos_available"] = False
        self._path_queue = queue
        self._path_cum = [0.0]
        for k in range(1, len(queue)):
            self._path_cum.append(
                self._path_cum[-1]
                + math.hypot(
                    queue[k][0] - queue[k - 1][0],
                    queue[k][1] - queue[k - 1][1],
                )
            )
        self._path_total = self._path_cum[-1]
        # Negative = phase A (approach to waypoint 1); the clock starts
        # at 0.0 once the robot reaches the path start.
        self._t_elapsed = -1.0 if self._leg_approach else 0.0
        self._mode = "path"
        # Fresh visualisation trail per run: the canvas shows THIS run only
        # (previous runs' strokes would otherwise accumulate and confuse).
        self._trail = [(self._x, self._z)]

    def set_time_scale(self, value: float) -> float:
        """Set the visible-simulation playback speed (clamped 0.1..10)."""
        self._time_scale = min(10.0, max(0.1, float(value)))
        return self._time_scale

    @property
    def time_scale(self) -> float:
        return self._time_scale

    async def halt_path(self) -> None:
        self._flags["ack_path_halt"] = True
        self._stop_motion()

    async def hold_path(self) -> None:
        self._stop_motion()

    async def stop_path(self) -> None:
        self._stop_motion()

    async def interrupt_path(self) -> None:
        self._stop_motion()

    def _stop_motion(self) -> None:
        self._mode = "idle"
        self._path_queue = []
        self._flags["path_execute"] = False
        self._vx = self._vz = 0.0

    async def home(self) -> None:
        """Edge-triggered like the plant: only a False->True transition counts."""
        if self._flags["home_execute"]:
            return  # latched: re-asserting True does nothing (observed)
        self._flags["home_execute"] = True
        self._flags["done"] = False
        self._flags["home_pos_available"] = False
        self._mode = "home"
        self._trail = [(self._x, self._z)]

    async def clear_home_flag(self) -> None:
        """Clear the latching ``g_xHomeExecute`` flag (part of the full
        homing recipe: False -> True -> wait -> False)."""
        self._flags["home_execute"] = False

    async def jog(self, axis: str, positive: bool, enabled: bool = True) -> None:
        axis = axis.lower()
        if axis == "x":
            pos_key, neg_key = "jog_x_pos", "jog_x_neg"
        elif axis == "z":
            pos_key, neg_key = "jog_z_pos", "jog_z_neg"
        else:
            raise ValueError(f"axis must be 'x' or 'z', got {axis!r}")
        self._flags[pos_key] = enabled and positive
        self._flags[neg_key] = enabled and not positive
        if enabled:
            self._mode = "jog"
        elif self._mode == "jog":
            self._mode = "idle"

    # ---------------------------------------------------- peripherals (API)
    async def pulse_gripper(self, duration: float = 0.5) -> None:
        if duration <= 0:
            raise ValueError("duration must be > 0")
        self._flags["gripper_ctrl_active"] = True
        try:
            await asyncio.sleep(duration)
        finally:
            self._flags["gripper_ctrl_active"] = False

    async def release_brake(self) -> None:
        self._flags["motor_brake_released"] = True

    async def reset_error(self) -> None:
        """Manual reset: clears a protection event immediately."""
        self._frozen_until = None
        self._flags["error"] = False
        self._flags["warning"] = False
        self._flags["motor_brake_released"] = True
        self._error_id = 0
        self._mode = "idle"
        self._vx = self._vz = 0.0

    # -------------------------------------------------------------- waits (API)
    async def _poll_until(self, predicate, timeout: float) -> bool:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if predicate():
                return True
            await asyncio.sleep(self.config.poll_interval)
        return predicate()

    async def wait_done(self, timeout: float = 30.0) -> bool:
        return await self._poll_until(lambda: self._flags["done"], timeout)

    async def wait_home_ready(self, timeout: float = 30.0) -> bool:
        return await self._poll_until(
            lambda: self._flags["home_pos_available"], timeout
        )

    async def wait_no_fault(self, timeout: float = 30.0) -> bool:
        return await self._poll_until(
            lambda: not self._flags["error"] and not self._flags["warning"],
            timeout,
        )

    async def wait_done_strict(self, timeout: float = 30.0) -> None:
        if not await self.wait_done(timeout):
            raise WaitTimeout(f"g_xDone not set within {timeout}s")

    # ----------------------------------------------------- sim-only helpers
    async def move_to_drawing_start(self) -> tuple[float, float]:
        """Home, then position at the lower-left corner without staging a drawing."""
        if self._positioning or self._mode != "idle" or self._flags["error"] or self._flags["warning"]:
            raise ValueError("Simulator must be idle and fault-free")
        target = (SIM_SAFE_BOX["x_min"], SIM_SAFE_BOX["z_min"])
        self._positioning = True
        self._positioning_abort = False
        try:
            await self.clear_home_flag()
            await self.home()
            try:
                await self._wait_for_positioning(SIM_HOME)
            finally:
                await self.clear_home_flag()
            self._prepare_path_run([dict(x_pos=target[0], z_pos=target[1],
                                         path_vel=50.0, path_acc=250.0, path_dec=250.0,
                                         blending_radius=0.0, x_halt=True)])
            # Live sequence: X+ at the home height first, then Z+ (Z+ at
            # x≈15 would run into the hull point (14, 23.3)).
            self._legs = [(0, target[0], True), (1, target[1], True)]
            self._leg_approach = 0
            self._t_elapsed = 0.0
            await self._wait_for_positioning(target)
            self._x, self._z = target
            return target
        finally:
            if self._mode in ("home", "path"):
                self._stop_motion()
            self._trail = [(self._x, self._z)]
            self._positioning = False

    async def _wait_for_positioning(self, target: tuple[float, float]) -> None:
        deadline = time.monotonic() + 45.0
        while time.monotonic() < deadline:
            if self._positioning_abort:
                raise ValueError("Drawing-start positioning cancelled")
            if self._flags["error"] or self._flags["warning"]:
                raise ValueError(f"Simulator positioning failed: status {self._error_id}")
            if self._mode == "idle":
                if self._flags["done"] and math.dist((self._x, self._z), target) <= 0.15:
                    return
                raise ValueError("Simulator positioning stopped before arrival")
            await asyncio.sleep(self.config.poll_interval)
        raise ValueError("Simulator positioning timed out")

    def reset(self) -> None:
        """Full state reset to a clean home (simulation only)."""
        self._seed()

    @property
    def trail(self) -> list[tuple[float, float]]:
        """Recorded path (x, z) pairs — for the visualisation."""
        return [] if self._positioning else list(self._trail)

    @property
    def positioning(self) -> bool:
        return self._positioning

    def cancel_positioning(self) -> None:
        self._positioning_abort = True

    @property
    def mode(self) -> str:
        """Current motion mode (``idle``/``jog``/``home``/``path``) for the UI."""
        return self._mode

    # ------------------------------------------------------------- internals
    @staticmethod
    def _validate_profile_index(index: int) -> None:
        if not 1 <= int(index) <= nodes.PROFILE_COUNT:
            raise ValueError(
                f"profile index must be 1..{nodes.PROFILE_COUNT}, got {index!r}"
            )


# --------------------------------------------------------------- dry runs
@dataclass
class SimResult:
    """Outcome of a headless dry run (see :func:`simulate_path`).

    ``trajectory`` is the single source of truth: a time-stamped
    ``(t, x, z)`` series that visualisation plays back interpolated and
    verification measures against the nominal path.
    """

    trajectory: list[tuple[float, float, float]]
    nominal: list[tuple[float, float]]  # intended path (blending-expanded)
    done: bool
    error: bool
    error_id: int
    events: list[tuple[float, str]]
    end: tuple[float, float]
    duration: float  # simulated seconds
    wall_time: float  # elapsed compute time
    route_start_index: int = 0


def simulate_path(
    waypoints: list[dict],
    start: tuple[float, float] | None = None,
    dt: float = 1.0 / 60.0,
    max_t: float = 900.0,
) -> SimResult:
    """Fast, HEADLESS dry run of a path — never touches the live robot.

    Spins a fresh :class:`SimRobot` through the exact same physics as the
    visible simulation, but synchronously and as fast as the CPU allows
    (no ticking, no sleeps, wall-clock independent).  ``waypoints`` are
    plain profile dicts (``x_pos``/``z_pos``/...).  Returns a
    :class:`SimResult` for verification and visualisation.
    """
    if not 1 <= len(waypoints) <= nodes.PROFILE_COUNT:
        raise ValueError(f"Expected 1..{nodes.PROFILE_COUNT} waypoints")
    # Jog drawing is slow (~10 u/s per axis + rests): allow long runs.
    if not (math.isfinite(dt) and dt >= 1 / 1000 and 0 < max_t <= 900):
        raise ValueError("Invalid simulation timestep or time limit")
    wall0 = time.monotonic()
    bot = SimRobot.__new__(SimRobot)
    bot._seed()
    if start is not None:
        bot._x, bot._z = float(start[0]), float(start[1])
        if not all(math.isfinite(v) for v in (bot._x, bot._z)):
            raise ValueError("Simulation origin must be finite")
    for i, w in enumerate(waypoints):
        p = bot._profiles[i]
        p["x_pos"] = float(w.get("x_pos", w.get("x", p["x_pos"])))
        p["z_pos"] = float(w.get("z_pos", w.get("z", p["z_pos"])))
        for key in ("path_vel", "path_acc", "path_dec", "blending_radius"):
            if key in w:
                p[key] = float(w[key])
        p["x_halt"] = i == len(waypoints) - 1
    # Same bank semantics as start_path: run slot 1..first x_halt.
    sel = []
    for p in bot._profiles:
        sel.append(p)
        if p["x_halt"]:
            break
    bot._prepare_path_run(sel)
    nominal = [(x, z) for (x, z, _v, _a) in bot._path_queue]

    traj: list[tuple[float, float, float]] = [(0.0, bot._x, bot._z)]
    events: list[tuple[float, str]] = []
    protected = False
    t = 0.0
    route_start_index = 0 if bot._path_total < 1e-6 else None
    while bot._mode == "path" and t < max_t:
        bot._step(min(dt, max_t - t))
        t = bot._clock
        if route_start_index is None and bot._t_elapsed >= 0:
            route_start_index = len(traj)
        if bot._frozen_until is not None and not protected:
            protected = True
            events.append((t, "Schutzreaktion 21005 (Grenzkontakt)"))
        traj.append((t, bot._x, bot._z))
    return SimResult(
        trajectory=traj,
        nominal=nominal,
        done=bool(bot._flags["done"]),
        error=protected,
        error_id=bot._error_id,
        events=events,
        end=(bot._x, bot._z),
        duration=t,
        wall_time=time.monotonic() - wall0,
        route_start_index=len(traj) if route_start_index is None else route_start_index,
    )
