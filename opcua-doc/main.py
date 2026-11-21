"""CLI entrypoint for the OPC UA metadata documentation tool.

Wires together the fixed module seams (``config``, ``connection``,
``services``, ``scan``, ``exports``). This file performs no OPC UA
service calls itself; all protocol interaction lives in the seams.

Exit codes:
    0    normal, complete run
    2    partial run (incomplete result from scan limits or data collection errors; reports still written)
    1    failed run (session, scan, or report writing failed)
    130  interrupted by Ctrl-C
"""

import asyncio
import sys

import config
import connection
import exports
import scan
import services

EXIT_OK = 0
EXIT_FAILED = 1
EXIT_PARTIAL = 2
EXIT_INTERRUPTED = 130


def _failure_message(exc: BaseException) -> str:
    """Return a short, sanitized description of a failure.

    Deliberately omits the exception message and traceback: transport-layer
    errors can embed endpoint addresses or credential fragments, which must
    not reach the console.
    """
    if isinstance(exc, asyncio.CancelledError):
        return "interrupted by cancellation"
    if isinstance(exc, TimeoutError):
        return "timed out"
    if isinstance(exc, ConnectionError):
        return "connection problem"
    if isinstance(exc, OSError):
        return "local system problem"
    return f"{type(exc).__name__}"


def _count(value) -> int:
    """Lenient cardinality for list/dict/set-shaped collections."""
    try:
        return len(value)
    except TypeError:
        return 0


def build_report(result: dict, options) -> dict:
    """Merge the request limits and a summary into the scan result."""
    report = dict(result)
    limits = dict(report.get("limits") or {})
    limits.update(
        {
            "max_requests": options.max_requests,
            "page_size": options.page_size,
            "request_interval": options.request_interval,
            "timeout": options.timeout,
            "scan_timeout": options.scan_timeout,
            "max_depth": options.max_depth,
            "max_nodes": options.max_nodes,
        }
    )
    report["limits"] = limits
    report["summary"] = {
        "nodes": _count(report.get("nodes")),
        "edges": _count(report.get("edges")),
        "issues": _count(report.get("issues")),
        "complete": bool(report.get("complete")),
    }
    return report


def _announce(paths, output_dir: str) -> None:
    """Print the written report paths (fall back to the output dir)."""
    if paths:
        for path in paths:
            print(path)
    else:
        print(output_dir)


async def run() -> int:
    """Execute one documentation run and return the process exit code."""
    options = config.parse_args()
    try:
        async with connection.open_session(options) as client:
            service = services.MetadataService(
                client,
                max_requests=options.max_requests,
                page_size=options.page_size,
                request_interval=options.request_interval,
                timeout=options.timeout,
                scan_timeout=options.scan_timeout,
            )
            result = await scan.scan(
                service,
                max_depth=options.max_depth,
                max_nodes=options.max_nodes,
                root=options.root,
            )
    except Exception as exc:  # details intentionally not propagated
        print(f"error: {_failure_message(exc)}", file=sys.stderr)
        return EXIT_FAILED

    # The session is closed here; persist the reports only afterwards.
    report = build_report(result, options)
    try:
        paths = exports.write_reports(report, options.output_dir)
    except Exception as exc:  # details intentionally not propagated
        print(f"error: {_failure_message(exc)}", file=sys.stderr)
        return EXIT_FAILED

    _announce(paths, options.output_dir)
    if report["summary"]["complete"]:
        return EXIT_OK
    print("warning: run finished partially (scan limits or data collection errors)", file=sys.stderr)
    return EXIT_PARTIAL


def main() -> int:
    try:
        return asyncio.run(run())
    except KeyboardInterrupt:
        print("interrupted", file=sys.stderr)
        return EXIT_INTERRUPTED


if __name__ == "__main__":
    sys.exit(main())
