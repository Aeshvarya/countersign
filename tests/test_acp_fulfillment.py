"""C10: the shipping address ACP sends is kept on the session and returned, never logged.

It is not bound into the terms hash, the passkey or the receipt: that is option (b)
in PROGRESS Open questions, a change to C1 that needs both builders.
"""

import json
import logging
import unittest
from datetime import datetime, timezone

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from countersign import receipt, tap
from countersign.fx import FakeFx
from countersign.merchant import FakeMerchant

try:
    from fastapi.testclient import TestClient

    from countersign.edge import create_app, hmac_auth
    from countersign.edge.acp import AcpConfig
except ImportError:  # the server extra is not installed
    TestClient = None

SECRET = b"k" * 32
SESSIONS = "/acp/checkout_sessions"

ADDRESS = {"name": "Asha Rao", "line_one": "12 Lake View Road", "line_two": "Flat 4B", "city": "Bengaluru",
           "state": "KA", "country": "IN", "postal_code": "560001"}


@unittest.skipIf(TestClient is None, "needs the server extra: pip install -e '.[server]' httpx")
class FulfillmentAddressTest(unittest.TestCase):
    def setUp(self):
        self.config = AcpConfig(secret=SECRET)
        self.merchant = FakeMerchant(FakeFx("84.10"))
        now = datetime(2026, 11, 2, 10, 0, tzinfo=timezone.utc)
        self.app = create_app(self.merchant, tap.StaticDirectory({}), authority="testserver", clock=lambda: now,
                              signer=receipt.MerchantSigner(Ed25519PrivateKey.generate(), "m-1"), acp=self.config)
        self.client = TestClient(self.app)
        self.sku = next(iter(self.merchant.catalog))
        self.keys = 0

    def call(self, method, path, body=None):
        raw = json.dumps(body).encode() if body is not None else b""
        h = dict(hmac_auth.sign(raw, SECRET, scheme=self.config.scheme))
        if body is not None:
            h["content-type"] = "application/json"
        if method == "POST":
            self.keys += 1
            h["Idempotency-Key"] = f"idem-{self.keys}"
        return self.client.request(method, path, content=raw if method == "POST" else None, headers=h)

    def create(self, **extra):
        r = self.call("POST", SESSIONS, {"items": [{"id": self.sku, "quantity": 1}], "currency": "INR", **extra})
        self.assertEqual(r.status_code, 201, r.text)
        return r.json()

    def update(self, sid, body):
        return self.call("POST", f"{SESSIONS}/{sid}", body)

    def test_the_address_on_create_comes_back_in_the_session(self):
        view = self.create(fulfillment_address=ADDRESS)
        self.assertEqual(view["fulfillment_address"], ADDRESS)
        self.assertEqual(self.call("GET", f"{SESSIONS}/{view['id']}").json()["fulfillment_address"], ADDRESS)

    def test_a_session_without_one_has_none(self):
        self.assertNotIn("fulfillment_address", self.create())

    def test_an_update_replaces_it(self):
        sid = self.create(fulfillment_address=ADDRESS)["id"]
        moved = {**ADDRESS, "line_one": "7 Hill Street", "line_two": None}
        r = self.update(sid, {"fulfillment_address": moved})
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(r.json()["fulfillment_address"], {k: v for k, v in moved.items() if v is not None})

    def test_only_acp_address_fields_are_kept(self):
        view = self.create(fulfillment_address={**ADDRESS, "notes": "leave at the door", "lat": 12.9})
        self.assertEqual(view["fulfillment_address"], ADDRESS)

    def test_a_malformed_address_is_refused_and_the_old_one_stays(self):
        sid = self.create(fulfillment_address=ADDRESS)["id"]
        for bad in ("12 Lake View Road", {"city": 560001}, {"city": "x" * 257}):
            with self.subTest(bad=bad):
                r = self.update(sid, {"fulfillment_address": bad})
                self.assertEqual(r.status_code, 400, r.text)
                self.assertTrue(r.json()["param"].startswith("$.fulfillment_address"))
                self.assertEqual(self.call("GET", f"{SESSIONS}/{sid}").json()["fulfillment_address"], ADDRESS)

    def test_the_address_is_never_logged(self):
        seen = []
        handler = logging.Handler(logging.DEBUG)
        handler.emit = lambda record: seen.append(record.getMessage())
        log = logging.getLogger("countersign")
        log.addHandler(handler)
        try:
            sid = self.create(fulfillment_address=ADDRESS)["id"]
            self.update(sid, {"fulfillment_address": {**ADDRESS, "city": 5}})  # a refused one too
            self.call("POST", f"{SESSIONS}/{sid}/complete", {"fulfillment_address": ADDRESS})
        finally:
            log.removeHandler(handler)
        text = json.dumps(seen)
        for value in ADDRESS.values():
            self.assertNotIn(value, text)


if __name__ == "__main__":
    unittest.main()
