"""Persistent OPC-UA session with low-level read/write for the SPS client.

Design notes
------------
* One long-lived session per :class:`PlcClient` (the future web app polls
  continuously; connecting per request would hammer the PLC).
* Manual connect sequence (socket -> hello -> channel -> session ->
  activate), identical to the proven pattern in ``opcua-doc/connection.py``:
  no watchdog, no auto-reconnect supervisor, no background data reads.
* Teardown is cancellation-safe: the socket is closed synchronously in a
  nested ``finally`` so it is released on every exit path.
* Writes are gated by ``PlcConfig.write_enabled`` (default OFF).  A
  :class:`PlcWriteDisabled` is raised before any write reaches the wire.
* A single failed operation triggers at most one automatic reconnect;
  repeated failures propagate to the caller.

The in-process :class:`FakeBackend` implements the same contract and lets
the whole stack be exercised without the plant (``dry_run=True``).
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import time
from typing import Any, Protocol

from asyncua import Client, ua
from asyncua.ua.uaerrors import BadNodeIdUnknown, UaError

from . import nodes
from .config import PlcConfig

logger = logging.getLogger("sps_client")

#: Exceptions that indicate a dead/broken transport and warrant one retry.
_RETRYABLE = (OSError, ConnectionError, asyncio.TimeoutError, UaError)


class PlcConnectionError(RuntimeError):
    """The OPC-UA transport could not be established or was lost."""


class PlcWriteDisabled(RuntimeError):
    """A write was attempted while ``write_enabled`` is False."""


class Backend(Protocol):
    """Transport contract shared by the real and the fake PLC."""

    @property
    def connected(self) -> bool: ...

    async def connect(self) -> None: ...
    async def close(self) -> None: ...
    async def read(self, node_id: ua.NodeId) -> Any: ...
    async def read_many(self, node_ids: list[ua.NodeId]) -> list[Any]: ...
    async def write(self, node_id: ua.NodeId, value: Any) -> None: ...


# ---------------------------------------------------------------------------
# Real OPC-UA backend
# ---------------------------------------------------------------------------
class OpuaBackend:
    """Thin wrapper around a persistent asyncua ``Client`` session."""

    def __init__(self, config: PlcConfig) -> None:
        self._config = config
        self._client: Client | None = None

    @property
    def connected(self) -> bool:
        return self._client is not None

    async def connect(self) -> None:
        cfg = self._config
        client = Client(cfg.endpoint, timeout=cfg.timeout)
        # Explicit: no supervisor task (connect() is never called).
        client._auto_reconnect = False
        try:
            if cfg.username:
                client.set_user(cfg.username)
                if cfg.password:
                    client.set_password(cfg.password)
            await client.connect_socket()
            await client.send_hello()
            await client.open_secure_channel()
            await client.create_session()
            await client.activate_session(
                username=client._username,
                password=client._password,
                certificate=client.user_certificate,
            )
        except BaseException:
            # A failed half-open connect must not leak the socket.
            with contextlib.suppress(Exception):
                client.disconnect_socket()
            raise
        self._client = client
        logger.info("OPC-UA session opened: %s", cfg.describe())

    async def close(self) -> None:
        client = self._client
        self._client = None
        if client is None:
            return
        try:
            with contextlib.suppress(Exception):
                await client.uaclient.close_session(True)
            with contextlib.suppress(Exception):
                await client.close_secure_channel()
        finally:
            # Synchronous floor: closes the socket even if the awaits above
            # were cancelled.
            with contextlib.suppress(Exception):
                client.disconnect_socket()
        logger.info("OPC-UA session closed")

    async def read(self, node_id: ua.NodeId) -> Any:
        assert self._client is not None
        return await self._client.get_node(node_id).read_value()

    async def read_many(self, node_ids: list[ua.NodeId]) -> list[Any]:
        assert self._client is not None
        node_objs = [self._client.get_node(nid) for nid in node_ids]
        return await self._client.read_values(node_objs)

    async def write(self, node_id: ua.NodeId, value: Any) -> None:
        assert self._client is not None
        await self._client.get_node(node_id).write_value(value)


# ---------------------------------------------------------------------------
# In-process fake PLC (dry run)
# ---------------------------------------------------------------------------
class FakeBackend:
    """Deterministic in-memory stand-in for the GVL_Exchange branch.

    Behaviour model (deliberately simple, for development only):

    * ``g_xPathExecute=True``  -> ``g_xDone`` rises after :data:`DONE_DELAY`.
    * ``g_xHomeExecute=True``  -> ``g_xHomePosAvailable`` rises after the delay.
    * ``g_xPathHalt/Hold/Stop/Interrupt=True`` -> motion stops (``g_xDone`` clears).
    * ``g_xMotorBrakeRelease=True`` -> ``g_xMotorBrakseReleased`` latches.
    * ``g_xPulseGripper=True`` -> ``g_xGripperCtrlActive`` mirrors it.
    * While a jog flag is set, the matching act value grows by 100 per read.
    """

    DONE_DELAY = 0.3
    JOG_STEP = 100

    def __init__(self) -> None:
        self._store: dict[str, Any] = {}
        self._connected = False
        self._t_path_start: float | None = None
        self._t_home_start: float | None = None
        self._brake_latched = False
        self._seed()

    def _seed(self) -> None:
        for name, nid in nodes.all_scalar_nodes().items():
            is_bool = name.startswith("g_x")
            self._store[nid.to_string()] = False if is_bool else 0

    @property
    def connected(self) -> bool:
        return self._connected

    async def connect(self) -> None:
        self._connected = True
        logger.info("FAKE PLC connected (dry run)")

    async def close(self) -> None:
        self._connected = False
        logger.info("FAKE PLC disconnected")

    def _dynamic(self, nid: ua.NodeId) -> Any:
        key = nid.to_string()
        value = self._store[key]
        now = time.monotonic()
        if nid == nodes.DONE:
            return self._t_path_start is not None and now - self._t_path_start >= self.DONE_DELAY
        if nid == nodes.ACK_PATH_HALT:
            return bool(self._store[nodes.PATH_HALT.to_string()])
        if nid == nodes.HOME_POS_AVAILABLE:
            return self._t_home_start is not None and now - self._t_home_start >= self.DONE_DELAY
        if nid == nodes.MOTOR_BRAKE_RELEASED:
            return self._brake_latched
        if nid == nodes.GRIPPER_CTRL_ACTIVE:
            return bool(self._store[nodes.GRIPPER_PULSE.to_string()])
        # Jog integration: while a direction flag is asserted, the matching
        # act value walks by JOG_STEP on every read of the act node.
        step = self.JOG_STEP
        if nid == nodes.ACT_X_POS:
            if self._store[nodes.JOG_X_POS.to_string()]:
                self._store[key] += step
            elif self._store[nodes.JOG_X_NEG.to_string()]:
                self._store[key] -= step
            return self._store[key]
        if nid == nodes.ACT_Z_POS:
            if self._store[nodes.JOG_Z_POS.to_string()]:
                self._store[key] += step
            elif self._store[nodes.JOG_Z_NEG.to_string()]:
                self._store[key] -= step
            return self._store[key]
        return value

    async def read(self, node_id: ua.NodeId) -> Any:
        self._require_connection()
        return self._dynamic(node_id)

    async def read_many(self, node_ids: list[ua.NodeId]) -> list[Any]:
        return [await self.read(nid) for nid in node_ids]

    async def write(self, node_id: ua.NodeId, value: Any) -> None:
        self._require_connection()
        key = node_id.to_string()
        if key not in self._store:
            raise KeyError(f"unknown node in fake PLC: {key}")
        self._store[key] = value
        now = time.monotonic()
        if node_id == nodes.PATH_EXECUTE and value:
            self._t_path_start = now
        if node_id in (
            nodes.PATH_HALT, nodes.PATH_HOLD, nodes.PATH_STOP, nodes.PATH_INTERRUPT
        ) and value:
            self._t_path_start = None
        if node_id == nodes.HOME_EXECUTE and value:
            self._t_home_start = now
        if node_id == nodes.BRAKE_RELEASE and value:
            self._brake_latched = True

    def _require_connection(self) -> None:
        if not self._connected:
            raise PlcConnectionError("fake PLC is not connected")


# ---------------------------------------------------------------------------
# Facade with lifecycle, gating, and one-shot reconnect
# ---------------------------------------------------------------------------
class PlcClient:
    """Low-level OPC-UA access: connect/close, read, write.

    Higher-level robot semantics live in :class:`sps_client.robot.Robot`.
    """

    def __init__(self, config: PlcConfig | None = None) -> None:
        self.config = config if config is not None else PlcConfig()
        self._backend: Backend = FakeBackend() if self.config.dry_run else OpuaBackend(self.config)
        self._lock = asyncio.Lock()

    # -- lifecycle ---------------------------------------------------------
    async def connect(self) -> None:
        async with self._lock:
            if self._backend.connected:
                return
            try:
                await self._backend.connect()
            except _RETRYABLE as exc:
                raise PlcConnectionError(f"cannot reach PLC: {exc}") from exc

    async def close(self) -> None:
        async with self._lock:
            await self._backend.close()

    async def __aenter__(self) -> "PlcClient":
        await self.connect()
        return self

    async def __aexit__(self, *exc_info: object) -> None:
        await self.close()

    # -- guarded operations --------------------------------------------------
    async def read(self, node_id: ua.NodeId) -> Any:
        return await self._operating(lambda: self._backend.read(node_id))

    async def read_many(self, node_ids: list[ua.NodeId]) -> list[Any]:
        return await self._operating(lambda: self._backend.read_many(node_ids))

    async def write(self, node_id: ua.NodeId, value: Any) -> None:
        # The dry-run fake is a sandbox: writes are always allowed there.
        # Against the real plant, write_enabled must be set deliberately.
        if not (self.config.write_enabled or self.config.dry_run):
            raise PlcWriteDisabled(
                "writes are disabled (PlcConfig.write_enabled=False); "
                "enable deliberately via OPCUA_ALLOW_WRITES=1"
            )
        return await self._operating(lambda: self._backend.write(node_id, value))

    async def _operating(self, action: Any) -> Any:
        """Run ``action`` with an automatic single reconnect on transport loss."""
        for attempt in (1, 2):
            async with self._lock:
                if not self._backend.connected:
                    try:
                        await self._backend.connect()
                    except _RETRYABLE as exc:
                        raise PlcConnectionError(f"cannot (re)establish PLC session: {exc}") from exc
            try:
                return await action()
            except BadNodeIdUnknown:
                # Missing nodes do not indicate a broken session.
                raise
            except _RETRYABLE as exc:
                if attempt == 2:
                    if not isinstance(exc, UaError):
                        raise PlcConnectionError(f"PLC transport failed: {exc}") from exc
                    raise
                logger.warning("transport error (%s); reconnecting once", exc)
                async with self._lock:
                    with contextlib.suppress(Exception):
                        await self._backend.close()
