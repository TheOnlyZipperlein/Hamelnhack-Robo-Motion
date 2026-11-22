"""Offline tests for the persistent drawing (parts accumulate, explicit delete only)."""
import runpy
import sys
import time
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "sps-client"))


class DrawingPartTests(unittest.TestCase):
    def setUp(self):
        with patch.dict("os.environ", {"ROBOT_BACKEND": "sim"}):
            module = runpy.run_path(str(ROOT / "web-app" / "main.py"))
        self.state = module["route_actual"].__globals__
        self.client = module["app"].test_client()
        self.sim = self.state["SyncPlc"](self.state["PlcConfig"](), robot_cls=self.state["SimRobot"])
        self.addCleanup(self.sim.close)
        self.state.update(plc=self.sim, plc_ok=True, IS_SIM=True)
        self.sim.set_time_scale(10)

    def _run(self, target, start=(50, 50), **body):
        # Slot 1 = start of the stroke (approach = pen up), slot 2 = end.
        common = dict(path_vel=200, path_acc=1000, path_dec=1000, blending_radius=0)
        self.sim.set_profile(1, x_pos=start[0], z_pos=start[1], x_halt=False, **common)
        self.sim.set_profile(2, x_pos=target[0], z_pos=target[1], x_halt=True, **common)
        reply = self.client.post("/api/robot/path/start", json=body).json
        self.assertTrue(reply["ok"])
        self.assertTrue(self.client.post("/api/robot/wait", json={
            "condition": "done", "path_run": True, "timeout": 30}).json["ok"])
        return reply

    def _parts(self):
        return self.client.get("/api/drawing").json["parts"]

    def test_parts_accumulate_and_travel_draws_nothing(self):
        a = self._run((80, 60), label="Linie 1")["part"]
        self.assertEqual(a["label"], "Linie 1")
        self.assertIsNone(self._run((150, 60), start=(120, 60), draw=False)["part"])
        b = self._run((200, 120), start=(150, 90))["part"]
        parts = self._parts()
        self.assertEqual([p["id"] for p in parts], [a["id"], b["id"]])
        self.assertTrue(all(p["completed"] and len(p["trail"]) >= 2 for p in parts))
        # The approach (pen up) is cut off: each stroke begins at its slot 1.
        for part, start in zip(parts, ((50, 50), (150, 90))):
            x, z = part["trail"][0]
            self.assertLessEqual(max(abs(x - start[0]), abs(z - start[1])), 1.5)
            self.assertEqual(part["start"], list(start))

    def test_delete_is_explicit(self):
        a = self._run((80, 60))["part"]
        b = self._run((100, 80))["part"]
        self.assertEqual(self.client.post("/api/drawing/clear", json={}).status_code, 400)
        self.assertEqual(len(self._parts()), 2)
        self.assertEqual(self.client.post("/api/drawing/parts/999/delete").status_code, 404)
        self.assertTrue(self.client.post(f"/api/drawing/parts/{a['id']}/delete").json["ok"])
        self.assertEqual([p["id"] for p in self._parts()], [b["id"]])
        self.assertEqual(self.client.post("/api/drawing/clear", json={"confirm": True}).json["deleted"], 1)
        self.assertEqual(self._parts(), [])

    def test_running_part_cannot_be_deleted(self):
        self.sim.set_time_scale(0.2)
        self.sim.set_profile(1, x_pos=250, z_pos=140, path_vel=50, path_acc=200,
                             path_dec=200, blending_radius=0, x_halt=True)
        pid = self.client.post("/api/robot/path/start", json={}).json["part"]["id"]
        time.sleep(0.3)
        self.assertEqual(self.client.post(f"/api/drawing/parts/{pid}/delete").status_code, 409)
        self.assertEqual(self.client.post("/api/drawing/clear", json={"confirm": True}).status_code, 409)
        self.client.post("/api/robot/path/stop")


if __name__ == "__main__":
    unittest.main()
