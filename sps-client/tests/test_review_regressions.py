"""Offline regressions for the live adapter and the Flask route workflow."""
import runpy
import struct
import sys
import unittest
import zlib
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "sps-client"))

from asyncua import ua
from asyncua.ua.uaerrors import BadNodeIdUnknown

from sps_client import PlcConnectionError, Robot
from sps_client import nodes


class RobotTests(unittest.IsolatedAsyncioTestCase):
    async def test_motion_fields_preserve_doubles(self):
        robot = Robot()
        robot.read_many = AsyncMock(return_value=[15.025] * 22)
        self.assertEqual((await robot.positions()).act_x_pos, 15.025)
        self.assertEqual((await robot.get_profile(1)).blending_radius, 15.025)
        robot.write = AsyncMock()
        await robot.set_profile(1, x_pos=183.302, blending_radius=16.44)
        values = [call.args[1] for call in robot.write.await_args_list]
        self.assertEqual(values, [183.302, 16.44])
        self.assertTrue(all(ua.Variant(v).VariantType == ua.VariantType.Double for v in values))

    async def test_missing_optional_status_is_unknown(self):
        robot = Robot()
        robot.read_many = AsyncMock(return_value=[0, 0, 0, 0, None] + [0] * 18)
        self.assertIsNone((await robot.status()).home_pos_available)

    async def test_missing_node_does_not_reconnect(self):
        robot = Robot()
        backend = SimpleNamespace(connected=True, write=AsyncMock(side_effect=BadNodeIdUnknown), close=AsyncMock())
        robot._backend = backend
        robot.config.write_enabled = True
        with self.assertRaises(BadNodeIdUnknown):
            await robot.halt_path()
        backend.write.assert_awaited_once_with(nodes.PATH_HALT, True)
        backend.close.assert_not_called()


class WebTests(unittest.TestCase):
    def setUp(self):
        with patch.dict("os.environ", {"ROBOT_BACKEND": "live"}):
            module = runpy.run_path(str(ROOT / "web-app" / "main.py"))
        self.state = module["route_actual"].__globals__
        self.client = module["app"].test_client()

    def test_stop_cancels_running_drawing_start(self):
        calls = []
        self.state.update(plc_ok=True, plc=SimpleNamespace(
            positioning=True, cancel_positioning=lambda: calls.append("cancel"),
            stop_path=lambda: calls.append("stop")))
        self.assertEqual(self.client.post("/api/robot/home").status_code, 409)
        self.assertEqual(self.client.post("/api/robot/path/stop").json["cancelled"], "drawing-start")
        self.assertEqual(calls, ["cancel"])

    def test_live_tracking_reports_plc_position_and_measured_path(self):
        self.state.update(
            plc_ok=True,
            plc=SimpleNamespace(
                positions=lambda: SimpleNamespace(act_x_pos=40.25, act_z_pos=50.5,
                                                  act_x_vel=0.1, act_z_vel=0),
                status=lambda: SimpleNamespace(error=False, warning=False, error_id=0),
            ),
            _TRACK={"running": True, "samples": [(0.1, 40.0, 50.0)]},
        )
        payload = self.client.get("/api/robot/tracking").json
        self.assertEqual((payload["x"], payload["z"]), (40.25, 50.5))
        self.assertEqual(payload["trail"], [[40.0, 50.0]])

    def test_sim_drawing_start_from_inside_field_preserves_profiles(self):
        self.state["IS_SIM"] = True
        sim = self.state["SyncPlc"](self.state["PlcConfig"](), robot_cls=self.state["SimRobot"])
        self.addCleanup(sim.close)
        self.state.update(plc=sim, plc_ok=True)
        sim.set_time_scale(5)
        sim.set_profile(1, x_pos=90, z_pos=80, path_vel=100, path_acc=500, x_halt=True)
        sim.start_path()
        self.assertTrue(sim.wait_done(5))
        profile = sim.get_profile(1)
        reply = self.client.post("/api/robot/drawing-start")
        self.assertEqual(reply.json, {"ok": True, "x": 30.0, "z": 35.0})
        self.assertEqual(sim.get_profile(1), profile)
        self.assertEqual(self.client.get("/api/sim/state").json["trail"], [[30.0, 35.0]])
        self.assertEqual(self.client.get("/api/route/actual").json["n"], 0)

    def test_read_can_recover_availability(self):
        class Stub:
            config = SimpleNamespace(dry_run=False)

            def status(self):
                if not self.connected:
                    raise PlcConnectionError("offline")
                return {"done": False}

        self.state["plc"] = stub = Stub()
        stub.connected = False
        self.assertEqual(self.client.get("/api/robot/status").status_code, 503)
        self.assertEqual(self.client.post("/api/robot/path/start").status_code, 503)
        stub.connected = True
        self.assertEqual(self.client.get("/api/robot/status").status_code, 200)
        self.assertTrue(self.client.get("/api/health").json["plc"])

    def test_path_wait_ignores_stale_done(self):
        class Stub:
            states = iter([True, True, False, True])

            def start_path(self):
                pass

            def positions(self):
                return SimpleNamespace(act_x_pos=15.025, act_z_pos=-10.0)

            def status(self):
                return SimpleNamespace(done=next(self.states), error=False, warning=False)

        self.state.update(plc=Stub(), plc_ok=True)
        self.assertEqual(self.client.post("/api/robot/path/start").status_code, 200)
        reply = self.client.post("/api/robot/wait", json={"condition": "done", "path_run": True, "timeout": 2})
        self.assertTrue(reply.json["ok"])
        result = self.client.get("/api/route/actual").json
        self.assertTrue(result["completed"])
        self.assertEqual(result["n"], 3)
        self.assertEqual(result["samples"][0][1], 15.025)

    def test_mismatched_origin_does_not_emit_comparison(self):
        reply = self.client.post("/api/route/simulate", json={
            "start": {"x": 100, "z": 92},
            "segments": [{"kind": "line", "to": {"x": 200, "z": 92}}],
        })
        states = iter([False, False, True])
        stub = SimpleNamespace(
            start_path=lambda: None,
            positions=lambda: SimpleNamespace(act_x_pos=90, act_z_pos=92),
            status=lambda: SimpleNamespace(done=next(states), error=False, warning=False),
        )
        self.state.update(plc=stub, plc_ok=True)
        response = self.client.post("/api/robot/path/start", json={"preview_id": reply.json["image_id"]})
        self.assertEqual(response.status_code, 200)
        wait = self.client.post("/api/robot/wait", json={"condition": "done", "path_run": True, "timeout": 2})
        self.assertTrue(wait.json["ok"])
        actual = self.client.get("/api/route/actual").json
        self.assertEqual(actual["n"], 2)
        self.assertIn("compare_unavailable", actual)
        self.assertNotIn("compare", actual)

    def test_dry_run_png_without_plc(self):
        reply = self.client.post("/api/route/simulate", json={
            "start": {"x": 100, "z": 92},
            "segments": [{"kind": "line", "to": {"x": 200, "z": 92}}],
        })
        self.assertTrue(reply.json["ok"])
        response = self.client.get(reply.json["image_url"])
        self.assertEqual(response.mimetype, "image/png")
        data = response.data
        w, h = struct.unpack_from(">II", data, 16)
        offset, chunks = 8, []
        while offset < len(data):
            n = struct.unpack_from(">I", data, offset)[0]
            if data[offset + 4:offset + 8] == b"IDAT":
                chunks.append(data[offset + 8:offset + 8 + n])
            offset += 12 + n
        pixels = zlib.decompress(b"".join(chunks))
        stride = 1 + 3 * w
        self.assertEqual(len(pixels), h * stride)
        i = 20 * stride + 1 + 3 * 20
        self.assertEqual(pixels[i:i + 3], b"\xf8\xfa\xfc")


if __name__ == "__main__":
    unittest.main()
