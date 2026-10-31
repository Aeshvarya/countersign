import os
import time
import unittest
import unittest.mock

try:
    from fastapi.testclient import TestClient

    from countersign.web.app import app
except ImportError:  # the server extra is not installed
    TestClient = None


@unittest.skipIf(TestClient is None, "needs the server extra: pip install -e '.[server]' httpx")
class WebFlowTest(unittest.TestCase):
    def setUp(self):
        self.client = TestClient(app)

    def wait(self, sid, status, timeout=5):
        deadline = time.time() + timeout
        while time.time() < deadline:
            state = self.client.get(f"/api/sessions/{sid}").json()
            if state["status"] == status:
                return state
            time.sleep(0.05)
        self.fail(f"session never reached {status}: {state['status']}")

    def test_shop_approve_pay_and_verify_the_receipt(self):
        sid = self.client.post("/api/sessions", json={"request": "Buy me a kettle", "currency": "INR", "model": "scripted"}).json()["id"]
        state = self.wait(sid, "awaiting_approval")
        self.assertIsNotNone(state["pending"])
        self.client.post(f"/api/sessions/{sid}/approve", json={"ceiling": state["pending"]["suggested_ceiling"]})
        state = self.wait(sid, "done")
        self.assertEqual(len(state["orders"]), 1)
        receipt = state["receipts"][0]
        approval = next(a for a in state["approvals"].values() if a["hash"] == receipt["body"]["approval"]["hash"])
        out = self.client.post("/api/receipts/verify", json={"receipt": receipt, "approval": approval, "merchant_key": state["merchant_key"]}).json()
        self.assertTrue(out["ok"])

    def test_a_ceiling_below_the_shown_price_is_rejected(self):
        sid = self.client.post("/api/sessions", json={"request": "Buy me a kettle", "currency": "EUR", "model": "scripted"}).json()["id"]
        self.wait(sid, "awaiting_approval")
        self.assertEqual(self.client.post(f"/api/sessions/{sid}/approve", json={"ceiling": "1"}).status_code, 400)

    def test_the_hosts_scripted_setting_beats_the_visitors_choice(self):
        # With a key on the host, COUNTERSIGN_MODEL=scripted must still keep every visit free.
        env = {"COUNTERSIGN_MODEL": "scripted", "ANTHROPIC_API_KEY": "not-a-real-key"}
        with unittest.mock.patch.dict(os.environ, env):
            state = self.client.post("/api/sessions", json={"request": "Buy me a kettle", "model": "claude"}).json()
        self.assertEqual(state["model"], "scripted agent (offline)")

    def test_bots_are_refused_at_the_edge(self):
        sid = self.client.post("/api/sessions", json={"request": "Buy me a kettle", "model": "scripted"}).json()["id"]
        self.wait(sid, "awaiting_approval")
        edge = self.client.post(f"/api/sessions/{sid}/levers/bot").json()["edge"]
        self.assertEqual(sum(not e["ok"] for e in edge), 5)
        self.assertTrue(any("body does not match" in e["detail"] for e in edge))


if __name__ == "__main__":
    unittest.main()
