"""C10: a currency is one we can price, and an Airwallex outage is never a currency problem."""

import json
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


class Clock:
    def __init__(self):
        self.now = datetime(2026, 11, 2, 10, 0, tzinfo=timezone.utc)

    def __call__(self):
        return self.now


@unittest.skipIf(TestClient is None, "needs the server extra: pip install -e '.[server]' httpx")
class CurrencyTest(unittest.TestCase):
    def build(self, fx):
        self.merchant = FakeMerchant(fx)
        app = create_app(self.merchant, tap.StaticDirectory({}), authority="testserver", clock=Clock(),
                         signer=receipt.MerchantSigner(Ed25519PrivateKey.generate(), "m-1"),
                         acp=AcpConfig(secret=SECRET))
        self.app = app
        self.client = TestClient(app)
        self.sku, self.other = list(self.merchant.catalog)[:2]
        self.keys = 0

    def setUp(self):
        self.build(FakeFx("84.10"))

    def call(self, method, path, body=None, *, key=None):
        raw = json.dumps(body).encode() if body is not None else b""
        h = {"content-type": "application/json", **hmac_auth.sign(raw, SECRET)}
        if method == "POST":
            if key is None:
                self.keys += 1
                key = f"idem-{self.keys}"
            h["Idempotency-Key"] = key
        return self.client.request(method, path, content=raw if method == "POST" else None, headers=h)

    def create(self, currency="INR", **kw):
        return self.call("POST", SESSIONS, {"items": [{"id": self.sku, "quantity": 1}], "currency": currency}, **kw)

    def view(self, sid):
        return self.call("GET", f"{SESSIONS}/{sid}").json()

    # -- a refused update changes nothing

    def test_a_refused_update_leaves_the_session_as_it_was(self):
        before = self.create().json()
        r = self.call("POST", f"{SESSIONS}/{before['id']}",
                      {"items": [{"id": self.other, "quantity": 2}], "currency": "12"})
        self.assertEqual(r.status_code, 400, r.text)
        # The next quote must price what the shopper had, not the refused cart.
        after = self.call("POST", f"{SESSIONS}/{before['id']}", {}).json()
        self.assertEqual(after["line_items"], before["line_items"])
        self.assertEqual((after["currency"], after["totals"]), (before["currency"], before["totals"]))

    def test_a_refused_ceiling_leaves_the_cart_as_it_was(self):
        before = self.create().json()
        r = self.call("POST", f"{SESSIONS}/{before['id']}",
                      {"items": [{"id": self.other, "quantity": 2}], "ceiling": 1.5})
        self.assertEqual(r.status_code, 400, r.text)
        after = self.call("POST", f"{SESSIONS}/{before['id']}", {}).json()
        self.assertEqual(after["line_items"], before["line_items"])


if __name__ == "__main__":
    unittest.main()
