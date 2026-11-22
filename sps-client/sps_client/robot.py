"""Semantic robot-control layer on top of :class:`PlcClient`.

Every method maps to the documented ``GVL_Exchange`` variables (see
``opcua-doc/OPCUA_ROBOT_INTERFACE.md``).  Role assignments (who writes
what) are *hypotheses from naming*; they are not verified against the
PLC program. Numeric motion values are OPC UA Doubles; engineering units
and scale factors are not encoded here.

All waiting helpers poll with ``config.poll_interval`` and return
``False`` on timeout instead of raising, so callers can decide the
escalation behaviour (abort, alarm, retry).
"""

from __future__ import annotations

import asyncio
import logging
import math
import time
from dataclasses import dataclass, field
from typing import Any

from . import nodes
from .plc import PlcClient

__all__ = [
    "PositioningError",
    "Robot",
    "RobotStatus",
    "Positions",
    "ProfileParams",
    "WaitTimeout",
]


class WaitTimeout(TimeoutError):
    """Raised by the strict wait helpers when the condition never became true."""


class PositioningError(ValueError):
    """Drawing-start positioning aborted; all command flags were cleared."""


_log = logging.getLogger("sps_client")

# Warning 20434 (meaning unknown) latches at the end of every finished motion
# since the failed path starts of 22.11.2026; motions are clean. Operator
# decision: ignore it completely. Robot.status() reports it as no warning
# (raw id kept in RobotStatus.ignored_warning). No reset is issued for it.
IGNORED_WARNING_IDS = frozenset({20434})


def _dist(a: tuple[float, float], b: tuple[float, float]) -> float:
    return math.hypot(a[0] - b[0], a[1] - b[1])


@dataclass
class RobotStatus:
    """Snapshot of the status-side GVL signals (one batch read)."""

    done: bool
    error: bool
    warning: bool
    ack_path_halt: bool
    home_pos_available: bool | None
    motor_brake_released: bool
    gripper_ctrl_active: bool
    error_id: int
    stat_words: tuple[int, ...] = field(default=())
    ctrl_words: tuple[int, ...] = field(default=())
    timestamp: float = field(default_factory=time.time)
    # Raw id of a warning that was suppressed (see IGNORED_WARNING_IDS); 0 = none.
    ignored_warning: int = 0

    @property
    def fault(self) -> bool:
        return self.error or self.warning


@dataclass
class Positions:
    """Snapshot of set/act values (one batch read). Raw units, unscaled."""

    act_x_pos: float
    act_x_vel: float
    act_z_pos: float
    act_z_vel: float
    act_values: tuple[float, ...] = field(default=())
    set_values: tuple[float, ...] = field(default=())
    override: float = 0
    set_path_vel_out: float = 0


@dataclass
class ProfileParams:
    """One element of ``g_ascProfilePar`` (struct ``scProfilePar``)."""

    index: int
    x_pos: float
    z_pos: float
    path_vel: float
    path_acc: float
    path_dec: float
    blending_radius: float
    x_halt: bool


_FIELD_TO_KWARG = {
    "lrXPos": "x_pos",
    "lrZPos": "z_pos",
    "lrPathVel": "path_vel",
    "lrPathAcc": "path_acc",
    "lrPathDec": "path_dec",
    "lrBlendingRadius": "blending_radius",
    "xHalt": "x_halt",
}


class Robot(PlcClient):
    """High-level wrapper: one call per robot intent.

    Example (async)::

        async with Robot(PlcConfig(write_enabled=True)) as bot:
            bot.set_profile(1, x_pos=1000, z_pos=500, path_vel=100)
            await bot.start_path(1)
            await bot.wait_done(30)
    """

    # ------------------------------------------------------------------ reads
    async def status(self) -> RobotStatus:
        ids = [
            nodes.DONE, nodes.ERROR, nodes.WARNING, nodes.ACK_PATH_HALT,
            nodes.HOME_POS_AVAILABLE, nodes.MOTOR_BRAKE_RELEASED,
            nodes.GRIPPER_CTRL_ACTIVE, nodes.ERROR_ID,
            *nodes.STAT_WORDS, *nodes.CTRL_WORDS,
        ]
        vals = await self.read_many(ids)
        for idx in (0, 1, 2):
            if vals[idx] is None:
                raise ValueError(f"Required PLC status unavailable: {ids[idx]}")
        error, warning, error_id = bool(vals[1]), bool(vals[2]), int(vals[7])
        # Live 22.11.2026: the warning bit can rise a cycle before g_wErrorID is
        # written (seen: warning=True, id=0, clean again moments later). Re-read
        # briefly so the id resolves; an id that stays 0 is still a warning.
        for _ in range(5):
            if not (warning and not error and error_id == 0):
                break
            await asyncio.sleep(0.05)
            e, w, i = await self.read_many([nodes.ERROR, nodes.WARNING, nodes.ERROR_ID])
            error, warning, error_id = bool(e), bool(w), int(i or 0)
        ignored = 0
        if warning and not error and error_id in IGNORED_WARNING_IDS:
            # Operator decision 22.11.2026: this warning is ignored everywhere.
            warning, ignored, error_id = False, error_id, 0
        return RobotStatus(
            done=bool(vals[0]),
            error=error,
            warning=warning,
            ignored_warning=ignored,
            ack_path_halt=bool(vals[3]),
            home_pos_available=None if vals[4] is None else bool(vals[4]),
            motor_brake_released=bool(vals[5]),
            gripper_ctrl_active=bool(vals[6]),
            error_id=error_id,
            stat_words=tuple(int(v) for v in vals[8:15]),
            ctrl_words=tuple(int(v) for v in vals[15:23]),
        )

    async def positions(self) -> Positions:
        ids = [
            nodes.ACT_X_POS, nodes.ACT_X_VEL, nodes.ACT_Z_POS, nodes.ACT_Z_VEL,
            *nodes.ACT_VALUES, *nodes.SET_VALUES,
            nodes.OVERRIDE, nodes.SET_PATH_VEL_OUT,
        ]
        vals = await self.read_many(ids)
        return Positions(
            act_x_pos=float(vals[0]),
            act_x_vel=float(vals[1]),
            act_z_pos=float(vals[2]),
            act_z_vel=float(vals[3]),
            act_values=tuple(float(v) for v in vals[4:12]),
            set_values=tuple(float(v) for v in vals[12:20]),
            override=float(vals[20]),
            set_path_vel_out=float(vals[21]),
        )

    async def get_profile(self, index: int) -> ProfileParams:
        self._validate_profile_index(index)
        vals = await self.read_many(
            [nodes.profile_field(index, f) for f in nodes.PROFILE_FIELDS]
        )
        return ProfileParams(
            index=index,
            x_pos=float(vals[0]),
            z_pos=float(vals[1]),
            path_vel=float(vals[2]),
            path_acc=float(vals[3]),
            path_dec=float(vals[4]),
            blending_radius=float(vals[5]),
            x_halt=bool(vals[6]),
        )

    # --------------------------------------------------------------- profiles
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
        """Write the given fields of ``g_ascProfilePar[index]`` (partial update)."""
        self._validate_profile_index(index)
        kwargs = {
            "x_pos": x_pos, "z_pos": z_pos, "path_vel": path_vel,
            "path_acc": path_acc, "path_dec": path_dec,
            "blending_radius": blending_radius, "x_halt": x_halt,
        }
        for kwarg, value in kwargs.items():
            if value is None:
                continue
            fld = next(f for f, k in _FIELD_TO_KWARG.items() if k == kwarg)
            coerced = bool(value) if fld == "xHalt" else float(value)
            await self.write(nodes.profile_field(index, fld), coerced)

    # ---------------------------------------------------------------- motion
    async def start_path(
        self,
        profile_index: int | None = None,
        *,
        path_vel_out: float | None = None,
        override: float | None = None,
    ) -> None:
        """Trigger a path run.

        ``profile_index`` is validated for range but NOT written anywhere:
        how the PLC selects the active profile is part of the (unverified)
        handshake protocol and not derivable from the metadata.
        """
        if profile_index is not None:
            self._validate_profile_index(profile_index)
        if path_vel_out is not None:
            await self.write(nodes.SET_PATH_VEL_OUT, float(path_vel_out))
        if override is not None:
            await self.write(nodes.OVERRIDE, float(override))
        await self.write(nodes.PATH_EXECUTE, True)

    async def halt_path(self) -> None:
        await self.write(nodes.PATH_HALT, True)

    async def hold_path(self) -> None:
        await self.write(nodes.PATH_HOLD, True)

    async def stop_path(self) -> None:
        await self.write(nodes.PATH_STOP, True)

    async def interrupt_path(self) -> None:
        await self.write(nodes.PATH_INTERRUPT, True)

    async def home(self) -> None:
        """Start the homing procedure (``g_xHomeExecute``)."""
        await self.write(nodes.HOME_EXECUTE, True)

    async def clear_home_flag(self) -> None:
        """Clear ``g_xHomeExecute`` (edge-triggered, latching — must be
        reset after homing arrives, otherwise the next homing is ignored)."""
        await self.write(nodes.HOME_EXECUTE, False)

    async def jog(self, axis: str, positive: bool, enabled: bool = True) -> None:
        """Enable/disable manual jogging of axis ``"x"`` or ``"z"``.

        Both direction flags of the axis are written (symmetric), so a
        single call fully defines the jog state of that axis.
        """
        axis = axis.lower()
        if axis == "x":
            pos_node, neg_node = nodes.JOG_X_POS, nodes.JOG_X_NEG
        elif axis == "z":
            pos_node, neg_node = nodes.JOG_Z_POS, nodes.JOG_Z_NEG
        else:
            raise ValueError(f"axis must be 'x' or 'z', got {axis!r}")
        await self.write(pos_node, enabled and positive)
        await self.write(neg_node, enabled and not positive)

    # ------------------------------------------------------------ peripherals
    async def pulse_gripper(self, duration: float = 0.5) -> None:
        """Assert ``g_xPulseGripper`` for ``duration`` seconds, then release.

        The flag is cleared in a ``finally`` so the pulse is closed even if
        the caller is cancelled mid-pulse.
        """
        if duration <= 0:
            raise ValueError("duration must be > 0")
        await self.write(nodes.GRIPPER_PULSE, True)
        try:
            await asyncio.sleep(duration)
        finally:
            await self.write(nodes.GRIPPER_PULSE, False)

    async def release_brake(self) -> None:
        """Release the motor brake (``g_xMotorBrakeRelease``)."""
        await self.write(nodes.BRAKE_RELEASE, True)

    async def reset_error(self) -> None:
        """Request an error reset (``g_xErrorReset``)."""
        await self.write(nodes.ERROR_RESET, True)

    # ------------------------------------------------------------------ waits
    async def _poll_until(self, predicate: Any, timeout: float) -> bool:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if await predicate():
                return True
            await asyncio.sleep(self.config.poll_interval)
        return await predicate()

    async def wait_done(self, timeout: float = 30.0) -> bool:
        """Poll ``g_xDone`` until True; return False on timeout."""
        return await self._poll_until(lambda: self.read(nodes.DONE), timeout)

    async def wait_home_ready(self, timeout: float = 30.0) -> bool:
        """Poll ``g_xHomePosAvailable`` until True; return False on timeout."""
        return await self._poll_until(lambda: self.read(nodes.HOME_POS_AVAILABLE), timeout)

    async def wait_no_fault(self, timeout: float = 30.0) -> bool:
        """Poll until both ``g_xError`` and ``g_xWarning`` are False."""
        async def _ok() -> bool:
            return not (await self.status()).fault  # honours IGNORED_WARNING_IDS
        return await self._poll_until(_ok, timeout)

    async def wait_done_strict(self, timeout: float = 30.0) -> None:
        """Like :meth:`wait_done` but raises :class:`WaitTimeout`."""
        if not await self.wait_done(timeout):
            raise WaitTimeout(f"g_xDone not set within {timeout}s")

    # --------------------------------------------------------- drawing start
    # Live sequence (22.11.2026, see AGENTS.md "Zeichenstart"): homing, then
    # jog X+ out of the home corridor, then jog Z+ into the lower-left
    # drawing corner. Path commands from home proved unreliable (no motion
    # in the last 3 of 6 starts, stall at (20, -5) before that), so none are used. Any fault or stall aborts.
    DRAWING_START = (30.0, 35.0)
    _HOME = (15.0, -10.0)
    _COMMAND_FLAGS = (
        ("PathStop", nodes.PATH_STOP), ("PathExecute", nodes.PATH_EXECUTE),
        ("HomeExecute", nodes.HOME_EXECUTE), ("PathInterrupt", nodes.PATH_INTERRUPT),
        ("JogXPos", nodes.JOG_X_POS), ("JogXNeg", nodes.JOG_X_NEG),
        ("JogZPos", nodes.JOG_Z_POS), ("JogZNeg", nodes.JOG_Z_NEG),
    )

    @property
    def positioning(self) -> bool:
        return getattr(self, "_positioning", False)

    def cancel_positioning(self) -> None:
        """Ask a running :meth:`move_to_drawing_start` to stop (thread-safe flag)."""
        self._positioning_abort = True

    async def clear_command_flags(self) -> None:
        """Set all 8 command flags (jog/home/path) to False with read-back.

        Used by the UI reset: an aborted agent run may leave a jog flag set.
        """
        if self.positioning:
            raise PositioningError("Positioning running; cancel it first")
        await self._ds_clear_flags()

    async def move_to_drawing_start(self) -> tuple[float, float]:
        """Home, then move to the lower-left drawing corner without drawing.

        The transfer is jog-only (X+ out of the home corridor, then Z+ into the
        field); the profile bank is not touched. Every command flag is cleared
        with read-back on every exit path.
        """
        if self.positioning:
            raise PositioningError("Drawing-start positioning already running")
        self._positioning = True
        self._positioning_abort = False
        try:
            await self._ds_wait_rest("preflight", timeout=10.0)
            await self._ds_clear_flags()
            await self._ds_check_fault_free()
            await self._ds_home()
            await self._ds_wait_rest("post-homing", timeout=15.0)
            await self._ds_jog(0, nodes.JOG_X_POS, nodes.JOG_X_NEG, self.DRAWING_START[0])
            await self._ds_jog(1, nodes.JOG_Z_POS, nodes.JOG_Z_NEG, self.DRAWING_START[1])
            final = await self._ds_wait_rest("arrival", timeout=10.0)
            await self._ds_check_fault_free()
            if _dist(final, self.DRAWING_START) > 2.0:
                raise PositioningError(f"Arrival not confirmed: stage at {final}")
            return final
        finally:
            try:
                await self._ds_clear_flags()
            finally:
                self._positioning = False

    # See IGNORED_WARNING_IDS: status() already suppresses these; the guards
    # below re-check defensively. Any other warning and every g_xError abort.
    IGNORED_WARNINGS = tuple(IGNORED_WARNING_IDS)

    async def _ds_sample(
        self, *, homing: bool = False, strict: bool = False,
    ) -> tuple[RobotStatus, tuple[float, float], float]:
        """One guarded sample. ``strict`` is kept for call sites; 20434 is ignored either way."""
        if getattr(self, "_positioning_abort", False):
            raise PositioningError("Positioning cancelled (stop requested)")
        s, v = await self.status(), await self.positions()
        xy = (v.act_x_pos, v.act_z_pos)
        ignored = s.error_id in self.IGNORED_WARNINGS or (homing and s.error_id == 20396)
        if s.error or (s.warning and not ignored):
            raise PositioningError(
                f"PLC fault at {xy}: error={s.error} warning={s.warning} id={s.error_id}")
        return s, xy, max(abs(v.act_x_vel), abs(v.act_z_vel))

    async def _ds_check_fault_free(self) -> None:
        """Gate before setting a motion flag: no error, no non-ignored warning."""
        s = await self.status()
        if s.error or (s.warning and s.error_id not in self.IGNORED_WARNINGS):
            raise PositioningError(
                f"PLC fault before motion: error={s.error} warning={s.warning} id={s.error_id}")

    async def _ds_set(self, node: str, value: bool) -> None:
        await self.write(node, value)
        if await self.read(node) is not value:
            raise PositioningError(f"Read-back mismatch for {node} (expected {value})")

    async def _ds_clear_flags(self) -> None:
        for _, node in self._COMMAND_FLAGS:
            if await self.read(node) is not False:
                await self._ds_set(node, False)

    async def _ds_wait_rest(self, label: str, timeout: float) -> tuple[float, float]:
        """Wait until positions span <= 0.002 per axis over ~1 s (allows settle)."""
        deadline = time.monotonic() + timeout
        window: list[tuple[float, tuple[float, float]]] = []
        while time.monotonic() < deadline:
            _, xy, _ = await self._ds_sample()
            now = time.monotonic()
            window = [(t, q) for t, q in window if now - t <= 1.0] + [(now, xy)]
            if len(window) >= 8 and all(
                max(q[a] for _, q in window) - min(q[a] for _, q in window) <= 0.002
                for a in (0, 1)
            ):
                return xy
            await asyncio.sleep(0.09)
        raise PositioningError(f"{label}: stage not at rest within {timeout:.0f}s")

    async def _ds_home(self) -> None:
        await self._ds_set(nodes.HOME_EXECUTE, False)
        await self._ds_set(nodes.HOME_EXECUTE, True)
        try:
            deadline = time.monotonic() + 60.0
            window: list[tuple[float, float]] = []
            while time.monotonic() < deadline:
                _, xy, _ = await self._ds_sample(homing=True, strict=True)
                if _dist(xy, self._HOME) <= 0.3:
                    window = (window + [xy])[-15:]
                    if len(window) == 15 and all(
                        max(q[a] for q in window) - min(q[a] for q in window) <= 0.02
                        for a in (0, 1)
                    ):
                        _log.info("drawing-start: homing settled at %s", xy)
                        return
                else:
                    window = []
                await asyncio.sleep(0.1)
            raise PositioningError("Homing did not settle at (15, -10) within 60 s")
        finally:
            await self._ds_set(nodes.HOME_EXECUTE, False)

    async def _ds_jog(self, axis: int, pos_node: str, neg_node: str, target: float) -> None:
        """Short jog bursts (<= 1 s) into a 2-unit band; read-back on every flag."""
        try:
            for _ in range(12):
                _, xy, _ = await self._ds_sample()
                cur = xy[axis]
                if abs(cur - target) <= 2.0:
                    return
                node = pos_node if target > cur else neg_node
                remaining = abs(target - cur)
                burst = 1.0 if remaining >= 8.0 else max(0.1, remaining / 10.0)
                await self._ds_check_fault_free()
                await self._ds_set(node, True)
                try:
                    end = time.monotonic() + burst
                    while time.monotonic() < end:
                        await self._ds_sample(strict=True)
                        await asyncio.sleep(0.05)
                finally:
                    await self._ds_set(node, False)
                await asyncio.sleep(0.3)
                _, xy, _ = await self._ds_sample()
                _log.info("drawing-start: jog burst %s -> %s", cur, xy)
                if abs(xy[axis] - cur) < 0.05:
                    raise PositioningError(f"Jog stalled at {xy}")
            raise PositioningError("Jog did not reach the target band")
        finally:
            for node in (pos_node, neg_node):
                if await self.read(node) is not False:
                    await self._ds_set(node, False)

    # ------------------------------------------------------------ jog drawing
    # Live 22.11.2026: g_xPathExecute from the drawing start produced no motion
    # (3 starts) and latched warning 20434. Drawings are therefore driven by
    # the jog flags, one axis at a time: axis-parallel segments exactly,
    # diagonals as a staircase. Jog speed ~10 u/s, stop overshoot ~0.6 u.
    SAFE_BOX = ((30.0, 260.0), (35.0, 150.0))
    _JOG_NODES = ((nodes.JOG_X_POS, nodes.JOG_X_NEG), (nodes.JOG_Z_POS, nodes.JOG_Z_NEG))

    async def bank_waypoints(self) -> list[tuple[float, float]]:
        """Waypoints in the profile bank, slot 1 up to the first ``x_halt``."""
        pts: list[tuple[float, float]] = []
        for i in range(1, nodes.PROFILE_COUNT + 1):
            p = await self.get_profile(i)
            pts.append((p.x_pos, p.z_pos))
            if p.x_halt:
                return pts
        raise PositioningError("Profile bank has no x_halt terminator; nothing to draw")

    async def draw_by_jog(
        self,
        points: list[tuple[float, float]],
        *,
        tol: float = 0.4,
        step: float = 2.0,
        on_leg: Any = None,
    ) -> tuple[float, float]:
        """Drive ``points`` with jog flags only (never ``g_xPathExecute``).

        The stage first moves from its current position to ``points[0]``
        (Z first when below the safe box, so it enters the field), then
        through the remaining points. Every point must lie in ``SAFE_BOX``.
        """
        (x0, x1), (z0, z1) = self.SAFE_BOX
        pts = [(float(x), float(z)) for x, z in points]
        if not pts:
            raise PositioningError("No waypoints to draw")
        for x, z in pts:
            if not (x0 <= x <= x1 and z0 <= z <= z1):
                raise PositioningError(f"Waypoint ({x}, {z}) outside safe box {self.SAFE_BOX}")
        if self.positioning:
            raise PositioningError("Positioning already running")
        self._positioning = True
        self._positioning_abort = False
        self._jd_lead = 0.06  # seconds of travel to release early (adaptive)
        try:
            cur = await self._ds_wait_rest("jog-draw preflight", timeout=10.0)
            if not (x0 - 1.0 <= cur[0] <= x1 + 1.0 and z0 - 1.0 <= cur[1] <= z1 + 1.0):
                raise PositioningError(f"Start position {cur} not at/in the safe box; run drawing start first")
            await self._ds_clear_flags()
            await self._ds_check_fault_free()
            # Approach: L-shape (both corners lie inside the convex box).
            first = pts[0]
            order = (1, 0) if cur[1] < z0 else (0, 1)
            for axis in order:
                cur = await self._jd_leg(axis, first[axis], tol)
            if on_leg:
                on_leg("start", cur)
            for target in pts[1:]:
                dx, dz = target[0] - cur[0], target[1] - cur[1]
                if abs(dx) <= tol or abs(dz) <= tol:
                    for axis in (0, 1):
                        if abs(target[axis] - cur[axis]) > tol:
                            cur = await self._jd_leg(axis, target[axis], tol)
                else:
                    n = max(1, math.ceil(max(abs(dx), abs(dz)) / step))
                    sx, sz = cur
                    for k in range(1, n + 1):
                        # Every step settles. Live 22.11.2026: starting the
                        # other axis right after a release (settle=False)
                        # coincided with 3 "did not settle" aborts and an
                        # axis that kept jogging X- -> never skip the rest.
                        cur = await self._jd_leg(0, sx + dx * k / n, tol)
                        cur = await self._jd_leg(1, sz + dz * k / n, tol)
                if on_leg:
                    on_leg("point", cur)
            end = await self._ds_wait_rest("jog-draw arrival", timeout=10.0)
            await self._ds_check_fault_free()
            return end
        finally:
            try:
                await self._ds_clear_flags()
            finally:
                self._positioning = False

    async def _jd_leg(self, axis: int, target: float, tol: float,
                      settle: bool = True) -> tuple[float, float]:
        """One single-axis jog to ``target``; releases early by the measured overshoot.

        ``settle=False`` (staircase steps): return right after the release
        instead of waiting for rest; no correction attempts, no lead update.
        """
        pos_node, neg_node = self._JOG_NODES[axis]
        (x0, x1), (z0, z1) = self.SAFE_BOX
        _, xy, _ = await self._ds_sample()
        other = xy[1 - axis]
        try:
            for attempt in range(4):
                _, xy, _ = await self._ds_sample()
                err = target - xy[axis]
                if abs(err) <= tol:
                    return xy
                node = pos_node if err > 0 else neg_node
                sign = 1.0 if err > 0 else -1.0
                timeout = abs(err) / 4.0 + 3.0
                await self._ds_check_fault_free()
                _, xy, _ = await self._ds_sample()
                start = xy[axis]
                await self._ds_set(node, True)
                t0 = time.monotonic()  # after the clear: stall/timeout count jog time only
                v_rel = 0.0
                try:
                    while True:
                        _, xy, v = await self._ds_sample(strict=True)
                        now = time.monotonic()
                        if not (x0 - 3 <= xy[0] <= x1 + 3 and z0 - 3 <= xy[1] <= z1 + 3) and \
                                abs(xy[axis] - start) > 0.5:
                            raise PositioningError(f"Left safe box during jog at {xy}")
                        if abs(xy[1 - axis] - other) > 1.5:
                            raise PositioningError(f"Off-axis drift during jog at {xy}")
                        if now - t0 > 1.0 and abs(xy[axis] - start) < 0.05:
                            raise PositioningError(f"Jog stalled at {xy}")
                        if now - t0 > timeout:
                            raise PositioningError(f"Jog leg timed out at {xy}")
                        remaining = sign * (target - xy[axis])
                        if remaining <= max(0.05, v * self._jd_lead):
                            v_rel, p_rel = v, xy[axis]
                            break
                        # Correction attempts: tiny pulse, no polling loop needed.
                        if attempt > 0:
                            v_rel, p_rel = v, xy[axis]
                            break
                finally:
                    await self._ds_set(node, False)
                if not settle:
                    return xy
                xy = await self._jd_settle()
                if attempt == 0 and v_rel > 2.0:
                    overshoot = sign * (xy[axis] - p_rel)
                    self._jd_lead = min(0.15, max(0.02, 0.5 * self._jd_lead + 0.5 * overshoot / v_rel))
                _log.info("jog-draw: axis %s -> %.3f target %.3f (lead %.3fs)",
                          "xz"[axis], xy[axis], target, self._jd_lead)
            _log.warning("jog-draw: axis %s ended %.3f off target %.3f",
                         "xz"[axis], xy[axis] - target, target)
            return xy
        finally:
            for node in (pos_node, neg_node):
                if await self.read(node) is not False:
                    await self._ds_set(node, False)

    async def _jd_settle(self, timeout: float = 3.0) -> tuple[float, float]:
        """Short rest check after releasing a jog flag.

        Same criterion as the live-verified original (span <= 0.01 over
        ~0.2 s), but polled every 12.5 ms on a sliding window, so rest is
        detected as soon as 0.2 s of stillness are seen.
        """
        deadline = time.monotonic() + timeout
        window: list[tuple[float, tuple[float, float]]] = []
        xy = (0.0, 0.0)
        while time.monotonic() < deadline:
            _, xy, _ = await self._ds_sample()
            now = time.monotonic()
            window = [(t, q) for t, q in window if now - t <= 0.2] + [(now, xy)]
            if len(window) >= 5 and now - window[0][0] >= 0.18 and all(
                max(q[a] for _, q in window) - min(q[a] for _, q in window) <= 0.01
                for a in (0, 1)
            ):
                return xy
            await asyncio.sleep(0.0125)
        raise PositioningError(f"Stage did not settle after jog (at {xy})")

    # -------------------------------------------------------------- internals
    @staticmethod
    def _validate_profile_index(index: int) -> None:
        if not 1 <= int(index) <= nodes.PROFILE_COUNT:
            raise ValueError(
                f"profile index must be 1..{nodes.PROFILE_COUNT}, got {index!r}"
            )
