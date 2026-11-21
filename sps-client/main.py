#!/usr/bin/env python3
"""CLI smoke test for the SPS client.

Commands:
    ping       connect + read one signal (connectivity check)
    status     print the status snapshot (repeat with --watch)
    positions  print the set/act value snapshot
    profiles   print all 10 motion profiles
    demo       scripted full sequence (dry-run by default)

Examples (from the repository root):

    .\\.venv\\Scripts\\python.exe sps-client\\main.py ping --dry-run
    .\\.venv\\Scripts\\python.exe sps-client\\main.py status --dry-run --watch
    .\\.venv\\Scripts\\python.exe sps-client\\main.py demo
    .\\.venv\\Scripts\\python.exe sps-client\\main.py status
"""

from __future__ import annotations

import argparse
import asyncio
import sys

from sps_client import PlcConfig, Robot


def build_config(args: argparse.Namespace) -> PlcConfig:
    overrides: dict[str, object] = {}
    if args.endpoint:
        overrides["endpoint"] = args.endpoint
    if args.dry_run:
        overrides["dry_run"] = True
    if args.allow_writes:
        overrides["write_enabled"] = True
    return PlcConfig.from_env(**overrides)


def banner(robot: Robot) -> None:
    print(f"[sps-client] {robot.config.describe()}")


def print_status(status) -> None:
    flags = [
        ("done", status.done),
        ("error", status.error),
        ("warning", status.warning),
        ("ack_path_halt", status.ack_path_halt),
        ("home_pos_available", status.home_pos_available),
        ("motor_brake_released", status.motor_brake_released),
        ("gripper_ctrl_active", status.gripper_ctrl_active),
    ]
    for name, value in flags:
        mark = "YES" if value else "-"
        print(f"  {name:<22} {mark}")
    print(f"  {'error_id':<22} {status.error_id}")
    print(f"  {'stat_words':<22} {list(status.stat_words)}")
    print(f"  {'ctrl_words':<22} {list(status.ctrl_words)}")


def print_positions(pos) -> None:
    print(f"  act_x_pos={pos.act_x_pos}  act_x_vel={pos.act_x_vel}")
    print(f"  act_z_pos={pos.act_z_pos}  act_z_vel={pos.act_z_vel}")
    print(f"  act_values={list(pos.act_values)}")
    print(f"  set_values={list(pos.set_values)}")
    print(f"  override={pos.override}  set_path_vel_out={pos.set_path_vel_out}")


async def cmd_ping(robot: Robot) -> None:
    from sps_client import nodes

    value = await robot.read(nodes.DONE)
    print(f"  OK – session alive, g_xDone={value!r}")


async def cmd_status(robot: Robot, args: argparse.Namespace) -> None:
    while True:
        print_status(await robot.status())
        if not args.watch:
            break
        print("-" * 40)
        await asyncio.sleep(max(args.interval, 0.1))


async def cmd_positions(robot: Robot, args: argparse.Namespace) -> None:
    print_positions(await robot.positions())


async def cmd_profiles(robot: Robot, args: argparse.Namespace) -> None:
    for idx in range(1, 11):  # 1..10 (see sps_client.nodes.PROFILE_COUNT)
        p = await robot.get_profile(idx)
        print(
            f"  [{idx:>2}] x={p.x_pos:<8} z={p.z_pos:<8} vel={p.path_vel:<6} "
            f"acc={p.path_acc:<6} dec={p.path_dec:<6} blend={p.blending_radius:<6} "
            f"halt={'Y' if p.x_halt else '-'}"
        )


async def cmd_demo(robot: Robot, args: argparse.Namespace) -> None:
    """Scripted end-to-end sequence against the fake PLC (or a real one)."""
    idx = args.profile_index
    print(f"-- demo: profile {idx}")
    await robot.set_profile(
        idx, x_pos=1000, z_pos=500, path_vel=100, path_acc=50, path_dec=50
    )
    p = await robot.get_profile(idx)
    print(f"   profile readback: x={p.x_pos} z={p.z_pos} vel={p.path_vel}")

    await robot.start_path(idx)
    print("-- path run started, waiting for g_xDone ...")
    ok = await robot.wait_done(args.timeout)
    print(f"   done={ok}")

    await robot.halt_path()
    print("-- halt requested")

    await robot.home()
    ok = await robot.wait_home_ready(args.timeout)
    print(f"   home_pos_available={ok}")

    await robot.pulse_gripper(0.3)
    print("-- gripper pulsed")

    await robot.release_brake()
    await robot.jog("x", positive=True)
    pos_a = await robot.positions()
    pos_b = await robot.positions()
    await robot.jog("x", positive=True, enabled=False)
    print(f"   jog x+ moved act_x_pos from {pos_a.act_x_pos} to {pos_b.act_x_pos}")

    st = await robot.status()
    print_status(st)
    print("-- demo finished")


async def run(args: argparse.Namespace) -> int:
    robot = Robot(build_config(args))
    try:
        await robot.connect()
        banner(robot)
        if args.command == "ping":
            await cmd_ping(robot)
        elif args.command == "status":
            await cmd_status(robot, args)
        elif args.command == "positions":
            await cmd_positions(robot, args)
        elif args.command == "profiles":
            await cmd_profiles(robot, args)
        elif args.command == "demo":
            await cmd_demo(robot, args)
        return 0
    except KeyboardInterrupt:
        print("\n[interrupted]")
        return 130
    finally:
        await robot.close()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="command", required=True)

    def add_common(p: argparse.ArgumentParser) -> None:
        p.add_argument("--endpoint", help="OPC-UA endpoint (default: from env/192.168.5.99:4840)")
        p.add_argument("--dry-run", action="store_true", help="use the in-process fake PLC")
        p.add_argument("--allow-writes", action="store_true", help="enable write access (careful!)")
        p.add_argument("--interval", type=float, default=1.0, help="watch interval in seconds")
        p.add_argument("--timeout", type=float, default=30.0, help="wait timeout in seconds")

    for name in ("ping", "status", "positions", "profiles", "demo"):
        p = sub.add_parser(name)
        add_common(p)
    sub.choices["status"].add_argument("--watch", action="store_true", help="update continuously")
    sub.choices["demo"].add_argument("--profile-index", type=int, default=1)

    args = parser.parse_args(argv)

    # Safety rail: demo writes; refuse a real PLC without explicit opt-in.
    if args.command == "demo" and not args.dry_run and not args.allow_writes:
        parser.error("demo needs --dry-run (default) or --allow-writes")

    try:
        return asyncio.run(run(args))
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    sys.exit(main())
