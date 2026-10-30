"""A7: one public host serves the demo, Airwallex's create_checkout, the shop window and the TAP door."""

import base64
import json
import unittest

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey, Ed25519PublicKey

from countersign import receipt, tap
from countersign.agent import AgentKey, InProcessShop

try:
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from countersign.edge import hmac_auth
    from countersign.web.host import KEY_PATH, Host, authority_of, house_from_env, signing_key_from
except ImportError:  # the server extra is not installed
    TestClient = None

SECRET = "s" * 32
SEED = base64.b64encode(bytes(range(32))).decode()
PUBLIC = "https://countersign.onrender.com"
AGENT_DIR = "https://agents.example"


def demo_app():
    demo = FastAPI()

    @demo.get("/")
    def index():
        return {"page": "demo"}

    return demo


@unittest.skipIf(TestClient is None, "needs the server extra: pip install -e '.[server]' httpx")
class HostTest(unittest.TestCase):
    def build(self, **env):
        environ = {"COUNTERSIGN_PUBLIC_URL": PUBLIC, "COUNTERSIGN_MERCHANT_SIGNING_KEY": SEED, **env}
        self.agent = Ed25519PrivateKey.generate()
        directory = tap.StaticDirectory({(AGENT_DIR, "k1"): self.agent.public_key()})
        self.house = house_from_env(environ, directory=directory)
        demo = demo_app()

        @demo.get(KEY_PATH)
        def key():
            return self.house.public_key()

        self.client = TestClient(Host(demo, self.house), base_url=PUBLIC)
        self.sku = next(iter(self.house.merchant.catalog))

    def acp(self, path, body, key):
        raw = json.dumps(body).encode()
        headers = {"content-type": "application/json", "Idempotency-Key": key,
                   **hmac_auth.sign(raw, SECRET.encode())}
        return self.client.post(path, content=raw, headers=headers)

    def test_the_demo_still_answers_everything_else(self):
        self.build()
        self.assertEqual(self.client.get("/").json(), {"page": "demo"})

    def test_the_shop_window_is_public(self):
        self.build()
        self.assertEqual(self.client.get(f"/p/{self.sku}").status_code, 200)
        r = self.client.get(f"/img/{self.sku}.svg")
        self.assertEqual((r.status_code, r.headers["content-type"]), (200, "image/svg+xml"))

    def test_create_checkout_end_to_end_with_a_receipt_anyone_can_verify(self):
        self.build(COUNTERSIGN_ACP_HMAC_SECRET=SECRET)
        self.assertTrue(self.house.acp)
        r = self.acp("/acp/checkout_sessions", {"items": [{"id": self.sku, "quantity": 1}], "currency": "INR"}, "c1")
        self.assertEqual(r.status_code, 201, r.text)
        session = r.json()
        self.assertEqual(session["status"], "ready_for_payment")
        done = self.acp(f"/acp/checkout_sessions/{session['id']}/complete", {}, "c2").json()
        self.assertEqual(done["status"], "completed", done)
        published = self.client.get(KEY_PATH).json()
        key = Ed25519PublicKey.from_public_bytes(base64.b64decode(published["public_key"]))
        self.assertEqual(published["key_id"], "merchant-key-1")
        self.assertTrue(receipt.verify(done["countersign"]["receipt"], key).ok)

    def test_an_unsigned_create_checkout_is_refused(self):
        self.build(COUNTERSIGN_ACP_HMAC_SECRET=SECRET)
        r = self.client.post("/acp/checkout_sessions", json={"items": []}, headers={"Idempotency-Key": "x"})
        self.assertEqual(r.status_code, 401)

    def test_without_a_secret_acp_stays_behind_tap(self):
        self.build()
        self.assertFalse(self.house.acp)
        self.assertEqual(self.client.post("/acp/checkout_sessions", json={}).status_code, 401)

    def test_the_tap_door_checks_against_the_public_host(self):
        self.build()
        self.assertEqual(self.client.get("/products?q=kettle").status_code, 401)
        shop = InProcessShop(self.client.app, AgentKey(self.agent, "k1", AGENT_DIR), authority_of(PUBLIC))
        self.assertTrue(shop.search("kettle", 3))

    def test_bad_configuration_stops_the_service(self):
        with self.assertRaises(ValueError):
            authority_of("http://plain.example")
        with self.assertRaises(ValueError):
            signing_key_from({"COUNTERSIGN_MERCHANT_SIGNING_KEY": base64.b64encode(b"short").decode()})
        with self.assertRaises(ValueError):
            house_from_env({"COUNTERSIGN_ACP_HMAC_SECRET": "too short"})

    def test_the_signing_key_survives_a_restart(self):
        a = signing_key_from({"COUNTERSIGN_MERCHANT_SIGNING_KEY": SEED}).public_key()
        b = signing_key_from({"COUNTERSIGN_MERCHANT_SIGNING_KEY": SEED}).public_key()
        self.assertEqual(a.public_bytes_raw(), b.public_bytes_raw())


if __name__ == "__main__":
    unittest.main()
