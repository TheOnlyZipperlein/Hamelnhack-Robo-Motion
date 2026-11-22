"""Offline checks for Robot.draw_by_jog (live drawing without g_xPathExecute).

A tiny kinematic model stands in for the PLC: a jog flag moves its axis at
SPEED units/s; releasing it adds a fixed overshoot, like the plant (~0.6 u).
"""

from __future__ import annotations

import asyncio
import sys
import threading
import time
import unittest
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from sps_client import PlcConfig, nodes  # noqa: E402
from sps_client.robot import PositioningError, Robot  # noqa: E402

SPEED = 40.0
OVERSHOOT_S = 0.02  # extra travel after release, in seconds of motion

JOG = {nodes.JOG_X_POS: (0, 1), nodes.JOG_X_NEG: (0, -1),
       nodes.JOG_Z_POS: (1, 1), nodes.JOG_Z_NEG: (1, -1)}


class KinematicRobot(Robot):
    def __init__(self, start=(30.44, 34.84), frozen=False, warn=None):
        super().__init__(PlcConfig(dry_run=True))
        self.pos = list(start)
        self.flags = {node: False for _, node in self._COMMAND_FLAGS}
        self.flags[nodes.ERROR_RESET] = False
        self.warn_mode = warn  # None | "after_leg" | "during_jog"
        self.warning_id = 0
        self.clears = 0
        self.frozen = frozen
        self.writes: list[tuple[str, bool]] = []
        self._t = time.monotonic()
        self._lock = threading.Lock()
        self._coast: list[tuple[int, float]] = []

    def _advance(self):
        with self._lock:
            now = time.monotonic()
            dt, self._t = now - self._t, now
            if self.frozen:
                return
            for node, (axis, sign) in JOG.items():
                if self.flags[node]:
                    self.pos[axis] += sign * SPEED * dt
            for axis, d in self._coast:
                self.pos[axis] += d
            self._coast = []

    async def status(self):
        self._advance()
        if self.warn_mode == "during_jog" and any(self.flags[n] for n in JOG):
            self.warning_id = 20434
        return SimpleNamespace(done=False, error=False, warning=bool(self.warning_id),
                               error_id=self.warning_id)

    async def positions(self):
        self._advance()
        moving = [JOG[n] for n, on in self.flags.items() if on and n in JOG]
        vx = SPEED if any(a == 0 for a, _ in moving) and not self.frozen else 0.0
        vz = SPEED if any(a == 1 for a, _ in moving) and not self.frozen else 0.0
        return SimpleNamespace(act_x_pos=self.pos[0], act_z_pos=self.pos[1],
                               act_x_vel=vx, act_z_vel=vz)

    async def read(self, node):
        return self.flags[node]

    async def write(self, node, value):
        self._advance()
        if node in JOG and self.flags[node] and not value and not self.frozen:
            axis, sign = JOG[node]
            self._coast.append((axis, sign * SPEED * OVERSHOOT_S))
            if self.warn_mode == "after_leg":
                self.warning_id = 20434
        if node == nodes.ERROR_RESET and self.flags[node] and not value and self.warning_id:
            self.warning_id = 0
            self.clears += 1
        self.flags[node] = value
        self.writes.append((str(node), value))


def run(coro):
    return asyncio.run(coro)


class JogDrawTests(unittest.TestCase):
    def assert_flags_clear(self, bot):
        self.assertFalse(any(bot.flags.values()), bot.flags)
        self.assertFalse(bot.positioning)

    def test_square_reaches_every_corner(self):
        bot = KinematicRobot()
        corners = []
        square = [(40, 45), (60, 45), (60, 60), (40, 60), (40, 45)]
        end = run(bot.draw_by_jog(square, on_leg=lambda kind, xy: corners.append(xy)))
        self.assertEqual(len(corners), len(square))
        for got, want in zip(corners, square):
            self.assertLessEqual(abs(got[0] - want[0]), 0.45, (got, want))
            self.assertLessEqual(abs(got[1] - want[1]), 0.45, (got, want))
        self.assertLessEqual(abs(end[0] - 40), 0.45)
        self.assert_flags_clear(bot)
        self.assertNotIn(str(nodes.PATH_EXECUTE), [n for n, v in bot.writes if v])

    def test_waypoint_outside_box_is_rejected_before_any_write(self):
        bot = KinematicRobot()
        with self.assertRaises(PositioningError):
            run(bot.draw_by_jog([(40, 45), (300, 45)]))
        self.assertEqual(bot.writes, [])

    def test_stall_aborts_and_clears_flags(self):
        bot = KinematicRobot(frozen=True)
        with self.assertRaisesRegex(PositioningError, "stalled"):
            run(bot.draw_by_jog([(40, 45)]))
        self.assert_flags_clear(bot)

    def test_warning_20434_is_ignored_without_reset(self):
        for mode in ("after_leg", "during_jog"):
            bot = KinematicRobot(warn=mode)
            end = run(bot.draw_by_jog([(40, 45), (50, 45)]))
            self.assertLessEqual(abs(end[0] - 50), 0.45, mode)
            self.assertEqual(bot.clears, 0, mode)
            self.assertNotIn(str(nodes.ERROR_RESET), [n for n, v in bot.writes if v], mode)
            self.assert_flags_clear(bot)

    def test_other_warning_while_jogging_aborts(self):
        bot = KinematicRobot(warn="during_jog")
        orig = bot.status

        async def status():
            s = await orig()
            if s.warning:
                s.error_id = 21005
            return s
        bot.status = status
        with self.assertRaisesRegex(PositioningError, "21005"):
            run(bot.draw_by_jog([(40, 45)]))
        self.assert_flags_clear(bot)

    def test_status_suppresses_20434_only(self):
        bot = Robot(PlcConfig(dry_run=True))

        async def fake_read_many(ids, wid=20434, err=False):
            vals = {nodes.ERROR: err, nodes.WARNING: True, nodes.ERROR_ID: wid}
            return [vals.get(n, 0) for n in ids]
        bot.read_many = fake_read_many
        s = run(bot.status())
        self.assertFalse(s.warning)
        self.assertFalse(s.fault)
        self.assertEqual((s.error_id, s.ignored_warning), (0, 20434))

        async def other(ids):
            return await fake_read_many(ids, wid=21005)
        bot.read_many = other
        s = run(bot.status())
        self.assertTrue(s.warning)
        self.assertEqual((s.error_id, s.ignored_warning), (21005, 0))

        def sequence(*ids_seq):
            seq = list(ids_seq)

            async def rm(ids):
                wid = seq.pop(0) if len(seq) > 1 else seq[0]
                if wid is None:
                    return [False if n in (nodes.ERROR, nodes.WARNING) else 0 for n in ids]
                return await fake_read_many(ids, wid=wid)
            return rm
        bot.read_many = sequence(0, 0, 20434)  # id written a cycle late
        s = run(bot.status())
        self.assertFalse(s.warning)
        self.assertEqual(s.ignored_warning, 20434)
        bot.read_many = sequence(0, None)  # transient warning, gone on re-read
        self.assertFalse(run(bot.status()).warning)
        bot.read_many = sequence(0)  # id never resolves -> still a warning
        self.assertTrue(run(bot.status()).warning)

        async def with_error(ids):
            return await fake_read_many(ids, err=True)
        bot.read_many = with_error
        s = run(bot.status())  # never hide 20434 when g_xError is set
        self.assertTrue(s.error and s.warning)
        self.assertEqual(s.error_id, 20434)

    def test_cancel_stops_and_clears_flags(self):
        bot = KinematicRobot()
        threading.Timer(0.3, bot.cancel_positioning).start()
        with self.assertRaisesRegex(PositioningError, "cancelled"):
            run(bot.draw_by_jog([(40, 45), (250, 45), (250, 140)]))
        self.assert_flags_clear(bot)

    def test_simulator_uses_the_same_legs_as_live(self):
        """SimRobot (jog_draw_legs) must drive the identical axis-parallel
        legs as Robot.draw_by_jog — straight up/down/left/right only."""
        from sps_client.simulator import jog_draw_legs
        start = (30.0, 35.0)
        pts = [(185, 92.5), (184.4, 99.4), (182.6, 106.2), (150, 140), (150, 60), (60, 60)]
        bot = KinematicRobot(start=start)
        recorded = []

        async def ideal_leg(axis, target, tol, settle=True):
            if abs(target - bot.pos[axis]) > tol:
                recorded.append((axis, round(target, 6), settle))
                bot.pos[axis] = target
            return tuple(bot.pos)
        bot._jd_leg = ideal_leg
        run(bot.draw_by_jog(pts))
        legs, approach = jog_draw_legs(start, pts)
        self.assertEqual(recorded, [(a, round(t, 6), s) for a, t, s in legs])
        self.assertEqual(approach, 2)


if __name__ == "__main__":
    unittest.main()
