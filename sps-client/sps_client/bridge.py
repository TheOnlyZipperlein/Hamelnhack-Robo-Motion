"""Blocking façade for synchronous frameworks (e.g. Flask).

Flask handlers run on the WSGI worker threads, not on an event loop.
:class:`SyncPlc` owns one dedicated thread with a permanent event loop and
forwards every call to the underlying :class:`~sps_client.robot.Robot`
via :func:`asyncio.run_coroutine_threadsafe`.

Usage sketch (Flask)::

    from sps_client import PlcConfig, SyncPlc

    plc = SyncPlc(PlcConfig())          # once, at app start

    @app.route("/status")
    def status():
        s = plc.status()                # blocking call
        return {"done": s.done, "error": s.error}

    # at shutdown:
    plc.close()
"""

from __future__ import annotations

import asyncio
import contextlib
import inspect
import threading
from typing import Any

from .config import PlcConfig
from .robot import Robot

__all__ = ["SyncPlc"]


class SyncPlc:
    """Thread-confined wrapper exposing :class:`Robot` as blocking calls.

    All coroutine-valued methods of the wrapped :class:`Robot` become
    synchronous methods here (delegated via ``__getattr__``); plain
    attributes (e.g. ``config``) are passed through untouched.
    """

    def __init__(self, config: PlcConfig | None = None, *, connect: bool = True) -> None:
        self._loop = asyncio.new_event_loop()
        self._thread = threading.Thread(
            target=self._run_loop, name="sps-client-loop", daemon=True
        )
        self._thread.start()
        self._robot = Robot(config)
        self._closed = False
        if connect:
            self.call(self._robot.connect())

    # -- plumbing ----------------------------------------------------------
    def _run_loop(self) -> None:
        asyncio.set_event_loop(self._loop)
        self._loop.run_forever()

    def call(self, coro: Any, timeout: float | None = None) -> Any:
        """Run ``coro`` on the client loop and block until it finishes."""
        if self._closed:
            raise RuntimeError("SyncPlc is closed")
        future = asyncio.run_coroutine_threadsafe(coro, self._loop)
        return future.result(timeout)

    def close(self) -> None:
        """Shut down the session and the backing thread (idempotent).

        The session is closed *before* ``_closed`` is flipped, because
        :meth:`call` refuses to run once the bridge is marked closed.
        """
        if self._closed:
            return
        with contextlib.suppress(Exception):
            future = asyncio.run_coroutine_threadsafe(self._robot.close(), self._loop)
            future.result(timeout=10)
        self._closed = True
        self._loop.call_soon_threadsafe(self._loop.stop)
        self._thread.join(timeout=5)

    # -- delegation ----------------------------------------------------------
    def __getattr__(self, name: str) -> Any:
        if name.startswith("_"):
            raise AttributeError(name)
        attr = getattr(self._robot, name)
        if inspect.iscoroutinefunction(attr):
            def wrapper(*args: Any, timeout: float | None = None, **kwargs: Any) -> Any:
                return self.call(attr(*args, **kwargs), timeout)
            return wrapper
        return attr
