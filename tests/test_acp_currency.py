"""C10: a currency is one we can price, and an Airwallex outage is never a currency problem."""

import http.client
import json
import unittest
import urllib.error
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
    def build(self, fx=None, merchant=None):
        self.merchant = merchant if merchant is not None else FakeMerchant(fx)
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

    def create(self, currency="INR", key=None, **extra):
        body = {"items": [{"id": self.sku, "quantity": 1}], "currency": currency, **extra}
        return self.call("POST", SESSIONS, body, key=key)

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

    def test_an_update_that_fails_unexpectedly_changes_nothing(self):
        fx = AirwallexLikeFx()
        self.build(fx)
        before = self.create("INR").json()
        fx.down = RuntimeError("a bug, not an outage")
        with self.assertRaises(RuntimeError):
            self.app.state.acp.update(before["id"], {"items": [{"id": self.other, "quantity": 2}], "currency": "EUR"})
        fx.down = None
        after = self.call("POST", f"{SESSIONS}/{before['id']}", {}).json()
        self.assertEqual((after["line_items"], after["currency"]), (before["line_items"], "inr"))

    def complete(self, sid, key="complete-1", body=None):
        return self.call("POST", f"{SESSIONS}/{sid}/complete", body or {}, key=key)

    def test_a_refused_complete_leaves_the_session_as_it_was(self):
        sid = self.call("POST", SESSIONS, {"items": [], "currency": "INR",
                                           "buyer": {"email": "first@example.com"}}).json()["id"]
        r = self.complete(sid, body={"buyer": {"email": "second@example.com"}, "payment_data": {"token": "tok_1"}})
        self.assertEqual((r.status_code, r.json()["code"]), (400, "invalid_state"))
        s = self.app.state.acp.sessions[sid]
        self.assertEqual((s.buyer, s.payment_data), ({"email": "first@example.com"}, None))

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
        for status, body in [(400, {"code": "validation_error", "source": "sell_currency"}),
                             (400, {"code": "validation_error", "source": "buy_currency"}),
                             (422, {"code": "validation_error", "source": "shopper_currency"}),
                             (400, {"code": "unsupported_currency", "message": "not offered"})]:
            with self.subTest(body=body):
                fx = AirwallexLikeFx()
                fx.refuse["XYZ"] = AirwallexError(status, body)
                self.build(fx)
                r = self.create("XYZ")
                self.assertEqual(r.status_code, 201, r.text)
                self.assertIn(body["code"], self.currency_message(r.json()))

    def test_a_400_that_does_not_name_a_currency_is_an_outage(self):
        # Our own fields, or the login before the call: not the shopper's currency.
        for status, body in [(400, {"code": "validation_error", "source": "validity_period"}),
                             (400, {"code": "validation_error", "source": "request_id"}),
                             (400, {"code": "credentials_invalid", "message": "check x-api-key"}),
                             (422, {"code": "validation_error", "message": "shopper_currency is fine"}),
                             (400, "<html>bad request</html>")]:
            with self.subTest(body=body):
                fx = AirwallexLikeFx()
                fx.refuse["INR"] = AirwallexError(status, body)
                self.build(fx)
                self.assert_unavailable(self.create("INR"))

    def test_an_unknown_product_is_still_no_product(self):
        r = self.call("POST", SESSIONS, {"items": [{"id": "NOPE-1", "quantity": 1}], "currency": "INR"})
        self.assertEqual(r.status_code, 201, r.text)
        self.assertEqual([(m["code"], m["param"]) for m in r.json()["messages"]], [("invalid", "$.items")])

    def test_a_shopper_can_move_to_a_currency_we_price(self):
        self.build(TableFx())
        sid = self.create("XYZ").json()["id"]
        view = self.call("POST", f"{SESSIONS}/{sid}", {"currency": "INR"}).json()
        self.assertEqual((view["status"], view["messages"]), ("ready_for_payment", []))

    # -- an outage is never a currency problem

    OUTAGES = [AirwallexError(401, {"code": "unauthorized"}), AirwallexError(403, {"code": "forbidden"}),
               AirwallexError(404, "not found"), AirwallexError(429, {"code": "too_many_requests"}),
               AirwallexError(500, "oops"), AirwallexError(503, {"code": "service_unavailable"}),
               urllib.error.URLError("connection refused"), TimeoutError("timed out"),
               ConnectionResetError("reset"), http.client.IncompleteRead(b"{\"id\""),
               json.JSONDecodeError("Expecting value", "<html>", 0),
               KeyError("id")]  # an answer without the field we read, not a missing product

    def assert_unavailable(self, r):
        self.assertEqual(r.status_code, 503, r.text)
        body = r.json()
        self.assertEqual((body["type"], body["code"]), ("service_unavailable", "rate_lock_unavailable"))
        self.assertNotIn("param", body)
        self.assertNotIn("currency", body["message"].lower())
        self.assertIn("Retry-After", r.headers)

    def test_an_outage_on_create_is_a_503_and_no_session(self):
        for err in self.OUTAGES:
            with self.subTest(err=repr(err)):
                fx = AirwallexLikeFx()
                fx.down = err
                self.build(fx)
                self.assert_unavailable(self.create("INR"))
                self.assertFalse(self.app.state.acp.sessions)

    def test_a_create_retried_after_the_outage_runs_again(self):
        fx = AirwallexLikeFx()
        fx.down = AirwallexError(503, "down")
        self.build(fx)
        self.assert_unavailable(self.create("INR", key="same"))
        fx.down = None
        r = self.create("INR", key="same")
        self.assertEqual(r.status_code, 201, r.text)
        self.assertNotIn("Idempotent-Replayed", r.headers)

    def test_an_outage_on_update_leaves_the_priced_session(self):
        fx = AirwallexLikeFx()
        self.build(fx)
        before = self.create("INR").json()
        fx.down = AirwallexError(401, {"code": "unauthorized"})
        self.assert_unavailable(self.call("POST", f"{SESSIONS}/{before['id']}", {"currency": "EUR"}))
        after = self.view(before["id"])
        self.assertEqual((after["status"], after["currency"], after["totals"]),
                         ("ready_for_payment", "inr", before["totals"]))


if __name__ == "__main__":
    unittest.main()
