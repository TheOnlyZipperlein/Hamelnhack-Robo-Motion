"""sps_client – SPS (PLC) client for robot control via OPC UA.

Public API:

* :class:`PlcConfig`   – settings (environment-driven, no secrets in code)
* :class:`PlcClient`   – low-level session (read/write, reconnect)
* :class:`Robot`       – semantic robot-control wrappers (recommended)
* :class:`SyncPlc`     – blocking façade for synchronous apps (Flask)
* :mod:`sps_client.nodes` – NodeId constants of ``GVL_Exchange``

See ``../README.md`` and ``../OPCUA_ROBOT_INTERFACE.md`` (in ``opcua-doc/``)
for the interface specification and safety notes.
"""

from .config import PlcConfig
from .plc import (
    PlcClient,
    PlcConnectionError,
    PlcWriteDisabled,
)
from .robot import (
    Positions,
    ProfileParams,
    Robot,
    RobotStatus,
    WaitTimeout,
)
from .bridge import SyncPlc

__version__ = "0.1.0"

__all__ = [
    "PlcConfig",
    "PlcClient",
    "PlcConnectionError",
    "PlcWriteDisabled",
    "Robot",
    "RobotStatus",
    "Positions",
    "ProfileParams",
    "WaitTimeout",
    "SyncPlc",
    "__version__",
]
