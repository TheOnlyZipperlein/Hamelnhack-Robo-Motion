"""NodeIds of the ``GVL_Exchange`` branch (c500).

Source of truth: the complete scan ``opcua-doc/out/run-20261121-172846``
and the specification ``opcua-doc/OPCUA_ROBOT_INTERFACE.md``.

Schema (observed in the scan):

    scalar variable      ns=4;s=|var|c500.Application.GVL_Exchange.<name>
    profile array member ns=4;s=|var|c500.Application.GVL_Exchange.g_ascProfilePar[N].<field>
    GVL root (object)    ns=4;s=|appo|c500.Application.GVL_Exchange

Role labels (command / status / set / act) are hypotheses from naming
conventions and are NOT verified against the PLC program.
"""

from __future__ import annotations

from asyncua import ua

#: Root of the exchange branch (object node, read-only).
GVL_ROOT = "ns=4;s=|appo|c500.Application.GVL_Exchange"

_VAR_PREFIX = "ns=4;s=|var|c500.Application.GVL_Exchange."


def var(name: str) -> ua.NodeId:
    """NodeId of a direct GVL_Exchange member variable."""
    return ua.NodeId.from_string(_VAR_PREFIX + name)


def profile_field(index: int, field: str) -> ua.NodeId:
    """NodeId of one field of one ``g_ascProfilePar[index]`` element.

    Valid indices are 1..10 (observed), valid fields are
    :data:`PROFILE_FIELDS`.
    """
    return ua.NodeId.from_string(f"{_VAR_PREFIX}g_ascProfilePar[{index}].{field}")


#: Number of motion profiles in ``g_ascProfilePar`` (observed: 10, index 1..10).
PROFILE_COUNT = 10

#: Field names of the struct type ``scProfilePar`` (observed order).
PROFILE_FIELDS: tuple[str, ...] = (
    "lrXPos",
    "lrZPos",
    "lrPathVel",
    "lrPathAcc",
    "lrPathDec",
    "lrBlendingRadius",
    "xHalt",
)

# ---------------------------------------------------------------------------
# Command signals (hypothesis: written by the robot controller)
# ---------------------------------------------------------------------------
PATH_EXECUTE = var("g_xPathExecute")
PATH_HALT = var("g_xPathHalt")
PATH_HOLD = var("g_xPathHold")
PATH_STOP = var("g_xPathStop")
PATH_INTERRUPT = var("g_xPathInterrupt")
HOME_EXECUTE = var("g_xHomeExecute")
ERROR_RESET = var("g_xErrorReset")
BRAKE_RELEASE = var("g_xMotorBrakeRelease")
GRIPPER_PULSE = var("g_xPulseGripper")
JOG_X_POS = var("g_xManJogXPos")
JOG_X_NEG = var("g_xManJogXNeg")
JOG_Z_POS = var("g_xManJogZPos")
JOG_Z_NEG = var("g_xManJogZNeg")

# ---------------------------------------------------------------------------
# Status signals (hypothesis: published by the PLC)
# ---------------------------------------------------------------------------
DONE = var("g_xDone")
ERROR = var("g_xError")
WARNING = var("g_xWarning")
ACK_PATH_HALT = var("g_xAckPathHalt")
HOME_POS_AVAILABLE = var("g_xHomePosAvailable")
# NOTE: the misspelling "Brakse" is in the plant, not here — keep it exact.
MOTOR_BRAKE_RELEASED = var("g_xMotorBrakseReleased")
GRIPPER_CTRL_ACTIVE = var("g_xGripperCtrlActive")

# ---------------------------------------------------------------------------
# Set / act values (Int64, raw scaled units — scale factor unknown)
# ---------------------------------------------------------------------------
OVERRIDE = var("g_lrOverride")
SET_PATH_VEL_OUT = var("g_lrSetPathVelOut")
ACT_X_POS = var("g_lrActXPos")
ACT_X_VEL = var("g_lrActXVel")
ACT_Z_POS = var("g_lrActZPos")
ACT_Z_VEL = var("g_lrActZVel")
ACT_VALUES: tuple[ua.NodeId, ...] = tuple(var(f"g_lrActValue{i}") for i in range(8))
SET_VALUES: tuple[ua.NodeId, ...] = tuple(var(f"g_lrSetValue{i}") for i in range(8))

# ---------------------------------------------------------------------------
# Control / status / error words (custom type ns=3;i=3002, definition unknown)
# ---------------------------------------------------------------------------
CTRL_WORDS: tuple[ua.NodeId, ...] = tuple(var(f"g_wCtrl{i}") for i in range(8))
STAT_WORDS: tuple[ua.NodeId, ...] = tuple(var(f"g_wStat{i}") for i in range(1, 8))
ERROR_ID = var("g_wErrorID")


def all_scalar_nodes() -> dict[str, ua.NodeId]:
    """Browse name -> NodeId for every scalar GVL member (plus profile fields).

    Used by the fake PLC to seed its memory.
    """
    names: dict[str, ua.NodeId] = {}
    singles = {
        "g_xDone": DONE, "g_xError": ERROR, "g_xWarning": WARNING,
        "g_xAckPathHalt": ACK_PATH_HALT, "g_xHomePosAvailable": HOME_POS_AVAILABLE,
        "g_xMotorBrakseReleased": MOTOR_BRAKE_RELEASED, "g_xGripperCtrlActive": GRIPPER_CTRL_ACTIVE,
        "g_xPathExecute": PATH_EXECUTE, "g_xPathHalt": PATH_HALT, "g_xPathHold": PATH_HOLD,
        "g_xPathStop": PATH_STOP, "g_xPathInterrupt": PATH_INTERRUPT,
        "g_xHomeExecute": HOME_EXECUTE, "g_xErrorReset": ERROR_RESET,
        "g_xMotorBrakeRelease": BRAKE_RELEASE, "g_xPulseGripper": GRIPPER_PULSE,
        "g_xManJogXPos": JOG_X_POS, "g_xManJogXNeg": JOG_X_NEG,
        "g_xManJogZPos": JOG_Z_POS, "g_xManJogZNeg": JOG_Z_NEG,
        "g_lrOverride": OVERRIDE, "g_lrSetPathVelOut": SET_PATH_VEL_OUT,
        "g_lrActXPos": ACT_X_POS, "g_lrActXVel": ACT_X_VEL,
        "g_lrActZPos": ACT_Z_POS, "g_lrActZVel": ACT_Z_VEL,
        "g_wErrorID": ERROR_ID,
    }
    names.update(singles)
    names.update(dict(zip(map(lambda n: f"g_lrActValue{n}", range(8)), ACT_VALUES)))
    names.update(dict(zip(map(lambda n: f"g_lrSetValue{n}", range(8)), SET_VALUES)))
    names.update(dict(zip(map(lambda n: f"g_wCtrl{n}", range(8)), CTRL_WORDS)))
    names.update(dict(zip(map(lambda n: f"g_wStat{n}", range(1, 8)), STAT_WORDS)))
    for idx in range(1, PROFILE_COUNT + 1):
        for field in PROFILE_FIELDS:
            names[f"g_ascProfilePar[{idx}].{field}"] = profile_field(idx, field)
    return names
