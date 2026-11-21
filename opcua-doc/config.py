"""Command-line configuration for the OPC UA address-space documentation tool.

``parse_args`` builds, parses, and cross-validates all CLI options.
This module performs no I/O and no network access: parsing and validation
only. Password acquisition (``OPCUA_PASSWORD`` env var or non-echo prompt),
certificate loading, and the connection itself belong to the connection
module (T3b+).
"""

from __future__ import annotations

import argparse
import math
import re
import sys
from pathlib import Path
from urllib.parse import urlsplit

from asyncua import ua

#: Default endpoint.
DEFAULT_ENDPOINT = "opc.tcp://192.168.5.99:4840"
#: Literal policy name meaning "no encryption".
INSECURE = "None"
#: Channel security modes.
SIGN = "Sign"
SIGN_AND_ENCRYPT = "SignAndEncrypt"
#: Env var holding the password for --username (never the CLI).
PASSWORD_ENV_VAR = "OPCUA_PASSWORD"
#: Protocol maximum for a single page size (OPC UA UInt32).
UINT32_MAX = 0xFFFFFFFF
#: Traversal is intentionally sequential; no parallel fetching exists.
TRAVERSAL_CONCURRENCY = 1

_POLICY_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_-]*$")

_EPILOG = """\
limits and security notes
  * Traversal is intentionally sequential (fixed concurrency 1); there is no
    option for parallel node fetching.
  * --page-size is bounded by the OPC UA protocol maximum (UInt32: 4294967295).
  * The password is never taken from the command line or the endpoint URL.
    With --username it is read from the OPCUA_PASSWORD env var if set;
    otherwise the connection module prompts without echoing.
  * Insecure connections (policy 'None') require explicit --allow-insecure.
  * Secure channels additionally require --client-certificate, --client-key
    and --server-certificate; certificate trust is confirmed out of band.
  * --user-certificate/--user-key form a pair, are mutually exclusive with
    --username, and require a secure channel.
  * Username/password auth is only permitted with --security-mode
    SignAndEncrypt to prevent credential downgrade.
  * --root restricts the scan to a subtree starting at the given NodeId;
    without it the scan starts at the standardized Objects folder.
"""


def _endpoint_url(value: str) -> str:
    """Validate an ``opc.tcp`` endpoint URL; reject userinfo/query/fragment."""
    try:
        parts = urlsplit(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError(str(exc)) from exc
    if parts.scheme != "opc.tcp":
        raise argparse.ArgumentTypeError(
            f"scheme must be 'opc.tcp', got '{parts.scheme or '<none>'}'")
    if parts.username is not None or parts.password is not None:
        raise argparse.ArgumentTypeError(
            "credentials (userinfo) are not allowed in the endpoint URL; "
            "the password is never taken from the CLI")
    if not parts.hostname:
        raise argparse.ArgumentTypeError("endpoint URL must include a host")
    if parts.query:
        raise argparse.ArgumentTypeError("query strings are not allowed")
    if parts.fragment:
        raise argparse.ArgumentTypeError("fragments are not allowed")
    return value


def _policy_name(value: str) -> str:
    """Accept syntactically valid policy names; support is checked later."""
    if not _POLICY_RE.match(value):
        raise argparse.ArgumentTypeError(
            f"'{value}' is not a syntactically valid security policy name")
    return value


def _finite_positive_float(value: str) -> float:
    try:
        num = float(value)
    except ValueError:
        raise argparse.ArgumentTypeError(f"'{value}' is not a number") from None
    if math.isnan(num) or math.isinf(num):
        raise argparse.ArgumentTypeError(f"{value} must be finite")
    if num <= 0:
        raise argparse.ArgumentTypeError(f"{value} must be greater than 0")
    return num


def _nonnegative_int(value: str) -> int:
    try:
        num = int(value)
    except ValueError:
        raise argparse.ArgumentTypeError(f"'{value}' is not an integer") from None
    if num < 0:
        raise argparse.ArgumentTypeError(f"{value} must be >= 0")
    return num


def _positive_int(value: str) -> int:
    try:
        num = int(value)
    except ValueError:
        raise argparse.ArgumentTypeError(f"'{value}' is not an integer") from None
    if num <= 0:
        raise argparse.ArgumentTypeError(f"{value} must be > 0")
    return num


def _page_size(value: str) -> int:
    num = _positive_int(value)
    if num > UINT32_MAX:
        raise argparse.ArgumentTypeError(
            f"{value} exceeds the protocol maximum (UInt32: {UINT32_MAX})")
    return num


def _node_id(value: str) -> ua.NodeId:
    """Parse an OPC UA NodeId string (e.g. ``ns=4;s=...`` or ``i=85``)."""
    try:
        return ua.NodeId.from_string(value)
    except Exception as exc:  # UaStringParsingError & co.: reject at the CLI
        raise argparse.ArgumentTypeError(
            f"'{value}' is not a valid NodeId string") from exc


class _CliParser(argparse.ArgumentParser):
    """Argument parser that exits with code 1 on usage errors.

    Exit code 2 is reserved for the "partial scan" contract, so argparse's
    default usage-error exit (2) is remapped to 1 to mean "error".
    """

    def error(self, message: str) -> None:
        self.print_usage(sys.stderr)
        self._print_message(message, sys.stderr)
        sys.exit(1)


def _build_parser() -> argparse.ArgumentParser:
    parser = _CliParser(
        prog="opcua-doc",
        description="Browse and document an OPC UA address space to disk.",
        epilog=_EPILOG,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--endpoint", type=_endpoint_url, default=DEFAULT_ENDPOINT,
                        help="OPC UA endpoint URL, scheme opc.tcp, no "
                             "userinfo/query/fragment (default: %(default)s)")
    parser.add_argument("--security-policy", type=_policy_name, default=INSECURE,
                        help="syntactic policy name, 'None' for insecure; the "
                             "connection module allows only supported secure "
                             "policies (default: %(default)s)")
    parser.add_argument("--security-mode", choices=(INSECURE, SIGN, SIGN_AND_ENCRYPT),
                        default=INSECURE,
                        help="channel security mode (default: %(default)s)")
    parser.add_argument("--allow-insecure", action="store_true",
                        help="explicit acknowledgement of an insecure "
                             "(unencrypted) connection; required when the "
                             "policy is 'None'")
    parser.add_argument("--username", default=None,
                        help="username; the password comes from the "
                             f"{PASSWORD_ENV_VAR} env var if set, else a "
                             "non-echo prompt (requires SignAndEncrypt)")
    parser.add_argument("--client-certificate", type=Path, default=None,
                        help="client X.509 certificate path; mandatory for "
                             "secure channels")
    parser.add_argument("--client-key", type=Path, default=None,
                        help="client private key path; mandatory for secure "
                             "channels")
    parser.add_argument("--server-certificate", type=Path, default=None,
                        help="trusted server certificate path; mandatory for "
                             "secure channels")
    parser.add_argument("--user-certificate", type=Path, default=None,
                        help="user X.509 certificate path; paired with "
                             "--user-key, exclusive with --username")
    parser.add_argument("--user-key", type=Path, default=None,
                        help="user private key path; paired with "
                             "--user-certificate")
    parser.add_argument("--application-uri", default=None,
                        help="application URI for channels that require a "
                             "matching client URI")
    parser.add_argument("--output-dir", type=Path, default=Path.cwd(),
                        help="output directory (default: current directory)")
    parser.add_argument("--timeout", type=_finite_positive_float, default=5.0,
                        help="operation timeout in seconds (default: %(default)s)")
    parser.add_argument("--max-depth", type=_nonnegative_int, default=2,
                        help="maximum browse depth (default: %(default)s)")
    parser.add_argument("--max-nodes", type=_positive_int, default=50,
                        help="maximum nodes to collect (default: %(default)s)")
    parser.add_argument("--root", type=_node_id, default=None,
                        help="start the scan at this NodeId instead of the "
                             "standard Objects folder; the node's class, "
                             "browse name and display name are identified "
                             "with one extra metadata read (default: Objects)")
    parser.add_argument("--max-requests", type=_positive_int, default=100,
                        help="maximum application requests (default: %(default)s)")
    parser.add_argument("--page-size", type=_page_size, default=20,
                        help="browse results per request, max UInt32 "
                             "(default: %(default)s)")
    parser.add_argument("--request-interval", type=_finite_positive_float,
                        default=0.25,
                        help="pause between requests in seconds "
                             "(default: %(default)s)")
    parser.add_argument("--scan-timeout", type=_finite_positive_float, default=60.0,
                        help="overall scan timeout in seconds (default: %(default)s)")
    return parser


def _cross_validate(args: argparse.Namespace, parser: argparse.ArgumentParser) -> None:
    policy = args.security_policy
    mode = args.security_mode
    secure = policy != INSECURE
    if mode != INSECURE and policy == INSECURE:
        parser.error(f"--security-mode {mode} requires a non-'None' --security-policy")
    if mode == INSECURE and policy != INSECURE:
        parser.error(
            f"--security-policy {policy!r} requires --security-mode "
            f"{SIGN} or {SIGN_AND_ENCRYPT}")
    if not secure:
        if not args.allow_insecure:
            parser.error("insecure connection (policy 'None') requires "
                         "explicit --allow-insecure")
    elif args.allow_insecure:
        parser.error("--allow-insecure is only valid for insecure "
                     "(policy 'None') connections")
    if secure:
        missing = [flag for flag, val in (
            ("--client-certificate", args.client_certificate),
            ("--client-key", args.client_key),
            ("--server-certificate", args.server_certificate),
        ) if val is None]
        if missing:
            parser.error("secure channel requires: " + ", ".join(missing))
    if args.username is not None:
        if mode != SIGN_AND_ENCRYPT:
            parser.error("--username (password authentication) requires "
                         f"--security-mode {SIGN_AND_ENCRYPT} to prevent "
                         "credential downgrade")
        if args.user_certificate is not None or args.user_key is not None:
            parser.error("--username is mutually exclusive with "
                         "--user-certificate/--user-key")
    if (args.user_certificate is None) != (args.user_key is None):
        parser.error("--user-certificate and --user-key must be given as a pair")
    if args.user_certificate is not None and not secure:
        parser.error("--user-certificate/--user-key require a secure channel "
                     "(policy != 'None')")


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    """Parse and validate CLI arguments.

    Returns a ``Namespace`` with attributes: ``endpoint`` (str),
    ``security_policy`` (str), ``security_mode`` (str), ``allow_insecure``
    (bool), ``username`` (str|None), ``client_certificate``, ``client_key``,
    ``server_certificate``, ``user_certificate``, ``user_key`` (Path|None),
    ``application_uri`` (str|None), ``output_dir`` (Path), ``timeout``,
    ``request_interval``, ``scan_timeout`` (float), ``max_depth``,
    ``max_nodes``, ``max_requests``, ``page_size`` (int), ``root``
    (ua.NodeId|None).
    Invalid input exits via ``argparse`` (usage + error, exit code 1).
    """
    parser = _build_parser()
    args = parser.parse_args(argv)
    _cross_validate(args, parser)
    return args
