"""C10: a currency is one we can price, and an Airwallex outage is never a currency problem."""

import json
import unittest
from datetime import datetime, timezone

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from countersign import receipt, tap
from countersign.airwallex import AirwallexError
from countersign.fx import FakeFx
from countersign.merchant import FakeMerchant

try:
    from fastapi.testclient import TestClient

    from countersign.edge import create_app, hmac_auth
    from countersign.edge.acp import AcpConfig
    from countersign.web.host import TableFx
except ImportError:  # the server extra is not installed
    TestClient = None

SECRET = b"k" * 32
SESSIONS = "/acp/checkout_sessions"


class Clock:
    def __init__(self):
        self.now = datetime(2026, 11, 2, 10, 0, tzinfo=timezone.utc)

    def __call__(self):
        return self.now


class AirwallexLikeFx:
    """Locks a rate, or raises what the Airwallex client raises: per currency, or for every call."""

    def __init__(self):
        self.ok = FakeFx("84.10")
        self.refuse: dict[str, Exception] = {}
        self.down: Exception | None = None

    def lock(self, merchant_currency, shopper_currency, now):
        if self.down is not None:
            raise self.down
        if shopper_currency in self.refuse:
            raise self.refuse[shopper_currency]
        return self.ok.lock(merchant_currency, shopper_currency, now)


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

    # -- a currency is one we can price

    def currency_message(self, view):
        self.assertEqual(view["status"], "not_ready_for_payment")
        self.assertEqual([m.get("param") for m in view["messages"]], ["$.currency"], view["messages"])
        msg = view["messages"][0]
        self.assertEqual((msg["type"], msg["code"]), ("error", "invalid"))
        self.assertEqual(view["line_items"], [])
        return msg["content"]

    def test_a_currency_the_house_has_no_rate_for_is_not_out_of_stock(self):
        self.build(TableFx())
        r = self.create("XYZ")
        self.assertEqual(r.status_code, 201, r.text)
        self.assertIn("XYZ", self.currency_message(r.json()))

    def test_out_of_stock_is_still_out_of_stock(self):
        self.build(TableFx())
        self.merchant.set_stock(self.sku, 0)
        view = self.create("INR").json()
        self.assertEqual([m["code"] for m in view["messages"]], ["out_of_stock"])

    def test_the_merchant_currency_needs_no_rate(self):
        self.build(TableFx())
        self.assertEqual(self.create("USD").json()["status"], "ready_for_payment")

    def test_airwallex_refusing_the_currency_is_a_currency_message(self):
        fx = AirwallexLikeFx()
        fx.refuse["XYZ"] = AirwallexError(400, {"code": "validation_error", "message": "shopper_currency"})
        self.build(fx)
        r = self.create("XYZ")
        self.assertEqual(r.status_code, 201, r.text)
        self.assertIn("validation_error", self.currency_message(r.json()))

    def test_a_shopper_can_move_to_a_currency_we_price(self):
        self.build(TableFx())
        sid = self.create("XYZ").json()["id"]
        view = self.call("POST", f"{SESSIONS}/{sid}", {"currency": "INR"}).json()
        self.assertEqual((view["status"], view["messages"]), ("ready_for_payment", []))


if __name__ == "__main__":
    unittest.main()
