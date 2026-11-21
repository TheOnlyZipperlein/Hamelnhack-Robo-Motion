"""Semantic robot-control layer on top of :class:`PlcClient`.

Every method maps to the documented ``GVL_Exchange`` variables (see
``opcua-doc/OPCUA_ROBOT_INTERFACE.md``).  Role assignments (who writes
what) are *hypotheses from naming*; they are not verified against the
PLC program.  Values are raw Int64/Boolean as exposed by the server —
engineering units and scale factors are NOT encoded here.

All waiting helpers poll with ``config.poll_interval`` and return
``False`` on timeout instead of raising, so callers can decide the
escalation behaviour (abort, alarm, retry).
"""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass, field
from typing import Any

from . import nodes
from .plc import PlcClient

__all__ = [
    "Robot",
    "RobotStatus",
    "Positions",
    "ProfileParams",
    "WaitTimeout",
]


class WaitTimeout(TimeoutError):
    """Raised by the strict wait helpers when the condition never became true."""


@dataclass
class RobotStatus:
    """Snapshot of the status-side GVL signals (one batch read)."""

    done: bool
    error: bool
    warning: bool
    ack_path_halt: bool
    home_pos_available: bool
    motor_brake_released: bool
    gripper_ctrl_active: bool
    error_id: int
    stat_words: tuple[int, ...] = field(default=())
    ctrl_words: tuple[int, ...] = field(default=())
    timestamp: float = field(default_factory=time.time)

    @property
    def fault(self) -> bool:
        return self.error or self.warning


@dataclass
class Positions:
    """Snapshot of set/act values (one batch read). Raw units, unscaled."""

    act_x_pos: int
    act_x_vel: int
    act_z_pos: int
    act_z_vel: int
    act_values: tuple[int, ...] = field(default=())
    set_values: tuple[int, ...] = field(default=())
    override: int = 0
    set_path_vel_out: int = 0


@dataclass
class ProfileParams:
    """One element of ``g_ascProfilePar`` (struct ``scProfilePar``)."""

    index: int
    x_pos: int
    z_pos: int
    path_vel: int
    path_acc: int
    path_dec: int
    blending_radius: int
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
        return RobotStatus(
            done=bool(vals[0]),
            error=bool(vals[1]),
            warning=bool(vals[2]),
            ack_path_halt=bool(vals[3]),
            home_pos_available=bool(vals[4]),
            motor_brake_released=bool(vals[5]),
            gripper_ctrl_active=bool(vals[6]),
            error_id=int(vals[7]),
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
            act_x_pos=int(vals[0]),
            act_x_vel=int(vals[1]),
            act_z_pos=int(vals[2]),
            act_z_vel=int(vals[3]),
            act_values=tuple(int(v) for v in vals[4:12]),
            set_values=tuple(int(v) for v in vals[12:20]),
            override=int(vals[20]),
            set_path_vel_out=int(vals[21]),
        )

    async def get_profile(self, index: int) -> ProfileParams:
        self._validate_profile_index(index)
        vals = await self.read_many(
            [nodes.profile_field(index, f) for f in nodes.PROFILE_FIELDS]
        )
        return ProfileParams(
            index=index,
            x_pos=int(vals[0]),
            z_pos=int(vals[1]),
            path_vel=int(vals[2]),
            path_acc=int(vals[3]),
            path_dec=int(vals[4]),
            blending_radius=int(vals[5]),
            x_halt=bool(vals[6]),
        )

    # --------------------------------------------------------------- profiles
    async def set_profile(
        self,
        index: int,
        *,
        x_pos: int | None = None,
        z_pos: int | None = None,
        path_vel: int | None = None,
        path_acc: int | None = None,
        path_dec: int | None = None,
        blending_radius: int | None = None,
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
            coerced = bool(value) if fld == "xHalt" else int(value)
            await self.write(nodes.profile_field(index, fld), coerced)

    # ---------------------------------------------------------------- motion
    async def start_path(
        self,
        profile_index: int | None = None,
        *,
        path_vel_out: int | None = None,
        override: int | None = None,
    ) -> None:
        """Trigger a path run.

        ``profile_index`` is validated for range but NOT written anywhere:
        how the PLC selects the active profile is part of the (unverified)
        handshake protocol and not derivable from the metadata.
        """
        if profile_index is not None:
            self._validate_profile_index(profile_index)
        if path_vel_out is not None:
            await self.write(nodes.SET_PATH_VEL_OUT, int(path_vel_out))
        if override is not None:
            await self.write(nodes.OVERRIDE, int(override))
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
            err, warn = await self.read_many([nodes.ERROR, nodes.WARNING])
            return not err and not warn
        return await self._poll_until(_ok, timeout)

    async def wait_done_strict(self, timeout: float = 30.0) -> None:
        """Like :meth:`wait_done` but raises :class:`WaitTimeout`."""
        if not await self.wait_done(timeout):
            raise WaitTimeout(f"g_xDone not set within {timeout}s")

    # -------------------------------------------------------------- internals
    @staticmethod
    def _validate_profile_index(index: int) -> None:
        if not 1 <= int(index) <= nodes.PROFILE_COUNT:
            raise ValueError(
                f"profile index must be 1..{nodes.PROFILE_COUNT}, got {index!r}"
            )
