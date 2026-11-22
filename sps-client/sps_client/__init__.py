"""sps_client – SPS (PLC) client for robot control via OPC UA.

Public API:

* :class:`PlcConfig`   – settings (environment-driven, no secrets in code)
* :class:`PlcClient`   – low-level session (read/write, reconnect)
* :class:`Robot`       – semantic robot-control wrappers (recommended)
* :class:`SyncPlc`     – blocking façade for synchronous apps (Flask)
* :class:`SimRobot`    – in-process simulator with the same API as :class:`Robot`
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
from .simulator import SIM_HOME, SIM_RAW_LIMITS, SIM_SAFE_BOX, SimRobot

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
    "SimRobot",
    "SIM_SAFE_BOX",
    "SIM_RAW_LIMITS",
    "SIM_HOME",
    "__version__",
]
