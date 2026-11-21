"""Runtime configuration for the SPS (PLC) client.

Every setting can be supplied via environment variables so that no
secret ever has to appear in source code, command lines, or URLs:

    OPCUA_ENDPOINT        opc.tcp endpoint (default: opc.tcp://192.168.5.99:4840)
    OPCUA_USERNAME        optional user name (anonymous if unset)
    OPCUA_PASSWORD        optional password (never echoed, never logged)
    OPCUA_TIMEOUT         per-request timeout in seconds (default: 5)
    OPCUA_POLL_INTERVAL   default polling interval in seconds (default: 0.1)
    OPCUA_ALLOW_WRITES    "1"/"true" enables write access (default: read-only)

The bootstrap build speaks SecurityPolicy None (anonymous).  Username
authentication is wired through but requires a signed/encrypted channel,
which is not negotiated yet (see README, section 8).
"""

from __future__ import annotations

import os
from dataclasses import dataclass

#: Plant endpoint observed during the documentation scans.
DEFAULT_ENDPOINT = "opc.tcp://192.168.5.99:4840"

_TRUTHY = {"1", "true", "yes", "on"}


def _env_flag(name: str) -> bool:
    return os.environ.get(name, "").strip().lower() in _TRUTHY


@dataclass
class PlcConfig:
    """Immutable-at-runtime settings for one :class:`PlcClient` instance."""

    endpoint: str = DEFAULT_ENDPOINT
    timeout: float = 5.0
    poll_interval: float = 0.1
    write_enabled: bool = False
    username: str | None = None
    password: str | None = None
    #: Use the in-process fake PLC (development/tests without the plant).
    dry_run: bool = False

    @classmethod
    def from_env(cls, **overrides: object) -> "PlcConfig":
        """Build a config from the environment, then apply keyword overrides.

        Overrides win over environment values, so callers can force
        ``dry_run=True`` or a test endpoint without touching the shell.
        """
        cfg = cls(
            endpoint=os.environ.get("OPCUA_ENDPOINT", DEFAULT_ENDPOINT),
            timeout=float(os.environ.get("OPCUA_TIMEOUT", "5")),
            poll_interval=float(os.environ.get("OPCUA_POLL_INTERVAL", "0.1")),
            write_enabled=_env_flag("OPCUA_ALLOW_WRITES"),
            username=os.environ.get("OPCUA_USERNAME") or None,
            password=os.environ.get("OPCUA_PASSWORD") or None,
        )
        for key, value in overrides.items():
            if not hasattr(cfg, key):
                raise AttributeError(f"unknown PlcConfig field: {key!r}")
            setattr(cfg, key, value)
        return cfg

    def describe(self) -> str:
        """One-line, secret-free description for logs and banners."""
        mode = "DRY-RUN (fake PLC)" if self.dry_run else self.endpoint
        writes = "schreibfreigabe AKTIV" if self.write_enabled else "read-only"
        user = f"user={self.username}" if self.username else "anonymous"
        return f"endpoint={mode} | {user} | {writes} | timeout={self.timeout}s"
