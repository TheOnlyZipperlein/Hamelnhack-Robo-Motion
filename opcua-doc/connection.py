"""Secure OPC-UA session manager for the address-space documentation tool.

``open_session`` is an async context manager that establishes a single
authenticated OPC-UA connection using asyncua 2.1.0's manual connect
sequence.  It deliberately avoids the high-level ``Client.connect()``
because that starts a watchdog task which periodically reads
``ServerStatus/State`` (a data-value read) and an auto-reconnect
supervisor — neither is appropriate for a short-lived documentation run.

Certificate trust is confirmed out of band; this module pins the server
certificate and refuses to proceed without it for secure channels.
No credential values are logged.
"""

from __future__ import annotations

import contextlib
import getpass
import logging
import os
from pathlib import Path
from typing import Any

from asyncua import Client, ua
from asyncua.crypto import security_policies

_logger = logging.getLogger(__name__)

#: Modern, non-deprecated security policies the tool will negotiate.
_ALLOWED_POLICIES: dict[str, type[security_policies.SecurityPolicy]] = {
    "Basic256Sha256": security_policies.SecurityPolicyBasic256Sha256,
    "Aes128Sha256RsaOaep": security_policies.SecurityPolicyAes128Sha256RsaOaep,
    "Aes256Sha256RsaPss": security_policies.SecurityPolicyAes256Sha256RsaPss,
}


def _resolve_password() -> str:
    """OPCUA_PASSWORD env var if set, else a non-echo terminal prompt."""
    env_val = os.environ.get("OPCUA_PASSWORD")
    if env_val:
        return env_val
    return getpass.getpass("OPC-UA password: ")


def _policy_class(name: str) -> type[security_policies.SecurityPolicy]:
    """Map an allowlisted policy name to its class; raise if unknown."""
    cls = _ALLOWED_POLICIES.get(name)
    if cls is None:
        allowed = ", ".join(sorted(_ALLOWED_POLICIES))
        raise ValueError(f"unsupported security policy {name!r}; allowed: {allowed}")
    return cls


async def _configure(client: Client, options: Any) -> None:
    """Apply security, identity, and application options to the Client."""
    if options.application_uri:
        client.application_uri = options.application_uri

    if options.security_policy == "None":
        return  # SecurityPolicyNone is already the Client default

    policy_cls = _policy_class(options.security_policy)
    mode = ua.MessageSecurityMode[options.security_mode]

    # Defense in depth: config already enforces this, guard here too.
    if options.username and mode != ua.MessageSecurityMode.SignAndEncrypt:
        raise ValueError("username authentication requires SignAndEncrypt")

    # Non-TOFU: explicit pinned server certificate is mandatory.
    if not options.server_certificate:
        raise ValueError("server-certificate is required for secure channels")
    if not options.client_certificate or not options.client_key:
        raise ValueError("client-certificate and client-key are required")

    await client.set_security(
        policy=policy_cls,
        certificate=str(options.client_certificate),
        private_key=str(options.client_key),
        server_certificate=str(options.server_certificate),
        mode=mode,
    )

    # Separate X.509 user identity (distinct from channel security).
    if options.user_certificate:
        await client.load_client_certificate(str(options.user_certificate))
    if options.user_key:
        await client.load_private_key(Path(str(options.user_key)))

    # Username / password: resolved from env or prompt, never from CLI.
    if options.username:
        client.set_user(options.username)
        client.set_password(_resolve_password())


async def _connect(client: Client) -> None:
    """Manual connect: socket → hello → channel → session → activate.

    Raises on the first failing step and performs no unwinding of its
    own: ``open_session`` is the single owner of teardown, so a failed
    or cancelled connect is cleaned up by exactly one code path.
    No watchdog, no supervisor, no data-value reads.
    """
    await client.connect_socket()
    await client.send_hello()
    await client.open_secure_channel()
    await client.create_session()
    await client.activate_session(
        username=client._username,
        password=client._password,
        certificate=client.user_certificate,
    )


async def _teardown_layers(client: Client) -> None:
    """Best-effort async teardown of the session and channel layers.

    Each step suppresses its own ordinary errors so a failure at one
    layer does not prevent the remaining layers from being cleaned up.
    Cancellation is *not* suppressed: if this coroutine is cancelled
    mid-await, the remaining async steps are skipped and the caller
    relies on the synchronous ``Client.disconnect_socket()`` to close
    the lowest layer.  Closing the socket ends the transport on our
    side; it does not guarantee that the server releases its side of
    the session or channel.
    """
    with contextlib.suppress(Exception):
        await client.uaclient.close_session(True)
    with contextlib.suppress(Exception):
        await client.close_secure_channel()


@contextlib.asynccontextmanager
async def open_session(options: Any) -> Client:
    """Yield a connected OPC-UA ``Client``; tear down on exit.

    ``options`` is the ``argparse.Namespace`` from ``config.parse_args``.
    Uses the manual connect sequence (no watchdog, no auto-reconnect,
    no data-value reads).

    This context manager is the single owner of teardown for both
    phases: a failed or cancelled setup and a finished or aborted body
    are cleaned up by the same ``finally``.  The session and channel
    are closed asynchronously on a best-effort basis; the socket is
    closed synchronously in a nested ``finally``, so it is closed even
    when a cancellation interrupts the async teardown.  Teardown
    faults are suppressed; note that a cancellation arriving during
    teardown propagates and supersedes any earlier body exception.
    """
    client = Client(options.endpoint, timeout=options.timeout)
    # Explicit: no supervisor task (defence in depth; connect() is never called).
    client._auto_reconnect = False

    try:
        await _configure(client, options)
        await _connect(client)
        yield client
    finally:
        # Async, best-effort: a cancellation during this await skips the
        # remaining async steps, and the nested finally below closes the
        # socket before the cancellation keeps propagating.
        try:
            await _teardown_layers(client)
        finally:
            # Synchronous, hence uninterruptible by cancellation: the
            # guaranteed floor of teardown on every exit path.  Suppressed
            # so a teardown fault can never mask the exception or
            # cancellation propagating through this finally.
            with contextlib.suppress(Exception):
                client.disconnect_socket()
