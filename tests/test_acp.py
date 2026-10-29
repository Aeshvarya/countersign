"""V10 / C10: the ACP checkout API Airwallex calls, end to end through the edge app."""

import json
import unittest
from datetime import datetime, timezone
from decimal import Decimal

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from countersign import receipt, tap
from countersign.fx import FakeFx
from countersign.merchant import FakeMerchant

try:
    from fastapi.testclient import TestClient

    from countersign.edge import create_app
    from countersign.edge import hmac_auth
    from countersign.edge.acp import AcpConfig, from_minor, to_minor
    from countersign.edge.app import MAX_BODY
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
class AcpTest(unittest.TestCase):
    config = AcpConfig(secret=SECRET) if TestClient else None

    def setUp(self):
        self.clock = Clock()
        self.fx = FakeFx("84.10")
        self.merchant = FakeMerchant(self.fx)
        self.merchant_key = Ed25519PrivateKey.generate()
        self.app = create_app(self.merchant, tap.StaticDirectory({}), authority="testserver", clock=self.clock,
                              signer=receipt.MerchantSigner(self.merchant_key, "m-1"), acp=self.config)
        self.client = TestClient(self.app)
        self.sku = next(iter(self.merchant.catalog))
        self.keys = 0

    def call(self, method, path, body=None, *, key=None, headers=None, sign=True):
        raw = json.dumps(body).encode() if body is not None else b""
        h = {"content-type": "application/json"} if body is not None else {}
        if sign:
            h.update(hmac_auth.sign(raw, SECRET, scheme=self.config.scheme))
        if method == "POST":
            if key is None:
                self.keys += 1
                key = f"idem-{self.keys}"
            if key:
                h["Idempotency-Key"] = key
        h.update(headers or {})
        return self.client.request(method, path, content=raw if method == "POST" else None, headers=h)

    def create(self, items=None, currency="INR", **extra):
        body = {"items": items if items is not None else [{"id": self.sku, "quantity": 1}], "currency": currency, **extra}
        r = self.call("POST", SESSIONS, body)
        self.assertEqual(r.status_code, 201, r.text)
        return r.json()

    def complete(self, sid, **kw):
        return self.call("POST", f"{SESSIONS}/{sid}/complete", {}, **kw)

    # -- authentication

    def test_an_unsigned_call_is_refused(self):
        r = self.call("POST", SESSIONS, {"items": []}, sign=False)
        self.assertEqual(r.status_code, 401)
        self.assertEqual(r.json()["code"], "unauthorized")
        self.assertFalse(self.app.state.acp.sessions)

    def test_a_tampered_body_is_refused(self):
        raw = json.dumps({"items": [{"id": self.sku, "quantity": 1}]}).encode()
        h = {**hmac_auth.sign(raw, SECRET), "Idempotency-Key": "t-1", "content-type": "application/json"}
        r = self.client.post(SESSIONS, content=raw.replace(b"1}", b"9}"), headers=h)
        self.assertEqual(r.status_code, 401)

    def test_tap_routes_are_unchanged(self):
        self.assertEqual(self.client.get("/products").status_code, 401)
        self.assertEqual(self.client.post("/checkout", json={}).status_code, 401)

    def test_the_body_cap_still_applies(self):
        r = self.client.post(SESSIONS, content=b"x" * (MAX_BODY + 1), headers={"Idempotency-Key": "big"})
        self.assertEqual(r.status_code, 413)

    # -- headers

    def test_every_post_needs_an_idempotency_key(self):
        r = self.call("POST", SESSIONS, {"items": []}, key="")
        self.assertEqual((r.status_code, r.json()["code"]), (400, "idempotency_key_required"))

    def test_an_unsupported_api_version_is_refused(self):
        r = self.call("POST", SESSIONS, {"items": []}, headers={"API-Version": "2025-01-01"})
        self.assertEqual(r.status_code, 400)
        self.assertEqual(r.json()["supported_versions"], ["2026-04-17"])
        ok = self.call("POST", SESSIONS, {"items": []}, headers={"API-Version": "2026-04-17"})
        self.assertEqual(ok.status_code, 201)

    def test_request_id_is_echoed(self):
        r = self.call("POST", SESSIONS, {"items": []}, headers={"Request-Id": "req-7"})
        self.assertEqual(r.headers["Request-Id"], "req-7")

    # -- the session

    def test_an_empty_cart_is_not_ready_for_payment(self):
        s = self.create(items=[])
        self.assertEqual(s["status"], "not_ready_for_payment")

    def test_a_priced_cart_is_ready_with_totals_in_the_shoppers_currency(self):
        s = self.create()
        self.assertEqual((s["status"], s["currency"]), ("ready_for_payment", "inr"))
        product = self.merchant.catalog[self.sku]
        total = next(t["amount"] for t in s["totals"] if t["type"] == "total")
        self.assertIsInstance(total, int)
        self.assertEqual(total, to_minor((product.price * Decimal("84.10")).quantize(Decimal("0.01")), "INR"))
        self.assertEqual(s["countersign"]["rate"], "84.10")
        self.assertTrue(s["countersign"]["terms_hash"])

    def test_create_update_complete_returns_an_order_and_a_verifiable_receipt(self):
        s = self.create()
        r = self.call("POST", f"{SESSIONS}/{s['id']}", {"items": [{"id": self.sku, "quantity": 2}],
                                                        "buyer": {"email": "shopper@example.com"}})
        self.assertEqual(r.json()["line_items"][0]["item"]["quantity"], 2)
        done = self.complete(s["id"]).json()
        self.assertEqual(done["status"], "completed")
        self.assertTrue(done["order"]["id"].startswith("ORD-"))
        self.assertEqual(done["order"]["checkout_session_id"], s["id"])
        check = receipt.verify(done["countersign"]["receipt"], self.merchant_key.public_key())
        self.assertTrue(check.ok, check.findings)
        self.assertEqual(done["countersign"]["receipt"]["body"]["approval"]["approver"], "acp:shopper@example.com")
        self.assertEqual(len(self.merchant.orders), 1)

    def test_a_price_change_before_complete_asks_again_and_charges_nothing(self):
        s = self.create()
        self.merchant.set_price(self.sku, self.merchant.catalog[self.sku].price * 2)
        r = self.complete(s["id"]).json()
        self.assertEqual(r["status"], "ready_for_payment")
        warning = next(m for m in r["messages"] if m["code"] == "price_change")
        self.assertEqual(warning["resolution"], "requires_buyer_review")
        self.assertGreater(warning["current_amount"], warning["previous_amount"])
        self.assertEqual(self.merchant.orders, {})
        # The shopper sees the new terms; completing them now is a fresh, informed yes.
        self.assertEqual(self.complete(s["id"]).json()["status"], "completed")

    def test_rate_drift_under_the_ceiling_proceeds(self):
        product_total = self.merchant.catalog[self.sku].price * Decimal("84.10")
        ceiling = to_minor((product_total * Decimal("1.02")).quantize(Decimal("0.01")), "INR")
        s = self.create(ceiling=ceiling)
        self.fx.rate = Decimal("84.94")  # +1%
        self.assertEqual(self.complete(s["id"]).json()["status"], "completed")

    def test_rate_drift_over_the_ceiling_asks_again(self):
        s = self.create()  # no ceiling: the approval is exactly the quoted total
        self.fx.rate = Decimal("88.30")  # +5%
        r = self.complete(s["id"]).json()
        self.assertEqual(r["status"], "ready_for_payment")
        self.assertIn("price_change", [m["code"] for m in r["messages"]])
        self.assertEqual(self.merchant.orders, {})

    def test_out_of_stock_at_complete_charges_nothing(self):
        s = self.create()
        self.merchant.set_stock(self.sku, 0)
        r = self.complete(s["id"]).json()
        self.assertEqual(r["status"], "not_ready_for_payment")
        self.assertIn("out_of_stock", [m["code"] for m in r["messages"]])
        self.assertEqual(self.merchant.orders, {})

    def test_an_unknown_product_is_reported_not_crashed(self):
        s = self.create(items=[{"id": "NOP-000-0", "quantity": 1}])
        self.assertEqual(s["status"], "not_ready_for_payment")
        self.assertEqual(s["messages"][0]["code"], "invalid")

    def test_a_delivery_change_asks_again(self):
        s = self.create()
        self.merchant.set_delivery(self.sku, "courier, next day")
        r = self.complete(s["id"]).json()
        self.assertEqual(r["status"], "ready_for_payment")
        self.assertIn("terms_changed", [m["code"] for m in r["messages"]])
        self.assertEqual(self.merchant.orders, {})

    def test_an_expired_approval_is_quoted_again(self):
        s = self.create()
        self.clock.now = self.clock.now.replace(minute=20)  # 20 minutes later, past the 15
        r = self.complete(s["id"]).json()
        self.assertEqual(r["status"], "ready_for_payment")
        self.assertIn("expired", [m["code"] for m in r["messages"]])
        self.assertEqual(self.merchant.orders, {})

    # -- idempotency and repeats

    def test_a_replayed_complete_returns_the_same_order_once(self):
        s = self.create()
        first = self.complete(s["id"], key="c-1")
        again = self.complete(s["id"], key="c-1")
        self.assertEqual(again.headers.get("Idempotent-Replayed"), "true")
        self.assertEqual(first.json(), again.json())
        self.assertEqual(len(self.merchant.orders), 1)

    def test_completing_again_with_a_new_key_never_charges_twice(self):
        s = self.create()
        self.complete(s["id"])
        r = self.complete(s["id"]).json()
        self.assertEqual(r["status"], "completed")
        self.assertEqual(len(self.merchant.orders), 1)

    def test_the_same_key_for_a_different_request_is_a_conflict(self):
        self.call("POST", SESSIONS, {"items": []}, key="dup")
        r = self.call("POST", SESSIONS, {"items": [{"id": self.sku, "quantity": 1}]}, key="dup")
        self.assertEqual((r.status_code, r.json()["code"]), (422, "idempotency_conflict"))

    def test_a_key_still_running_is_in_flight(self):
        api = self.app.state.acp
        body = {"items": []}
        from countersign.edge.idempotency import fingerprint
        api.idempotency.begin("busy", fingerprint("POST", SESSIONS, body))
        r = self.call("POST", SESSIONS, body, key="busy")
        self.assertEqual((r.status_code, r.json()["code"]), (409, "idempotency_in_flight"))
        self.assertEqual(r.headers["Retry-After"], "1")

    # -- lifecycle

    def test_cancel_then_nothing_else(self):
        s = self.create()
        self.assertEqual(self.call("POST", f"{SESSIONS}/{s['id']}/cancel", {}).json()["status"], "canceled")
        self.assertEqual(self.call("POST", f"{SESSIONS}/{s['id']}/cancel", {}).status_code, 405)
        self.assertEqual(self.call("POST", f"{SESSIONS}/{s['id']}", {"items": []}).status_code, 405)
        self.assertEqual(self.complete(s["id"]).status_code, 405)
        self.assertEqual(self.merchant.orders, {})

    def test_a_completed_session_cannot_be_canceled_or_changed(self):
        s = self.create()
        self.complete(s["id"])
        self.assertEqual(self.call("POST", f"{SESSIONS}/{s['id']}/cancel", {}).status_code, 405)
        self.assertEqual(self.call("POST", f"{SESSIONS}/{s['id']}", {"items": []}).status_code, 405)

    def test_get_and_unknown_sessions(self):
        s = self.create()
        self.assertEqual(self.call("GET", f"{SESSIONS}/{s['id']}").json()["id"], s["id"])
        self.assertEqual(self.call("GET", f"{SESSIONS}/cs_nope").status_code, 404)

    def test_bad_input_is_a_400_with_a_param(self):
        r = self.call("POST", SESSIONS, {"items": [{"id": self.sku, "quantity": 0}]})
        self.assertEqual((r.status_code, r.json()["param"]), (400, "$.items[0].quantity"))
        r = self.call("POST", SESSIONS, {"items": [], "ceiling": 12.5})
        self.assertEqual((r.status_code, r.json()["param"]), (400, "$.ceiling"))


@unittest.skipIf(TestClient is None, "needs the server extra")
class ShopperSignatureGateTest(unittest.TestCase):
    """With U1 switched on, nothing completes until the shopper has signed these exact terms."""

    def setUp(self):
        self.merchant = FakeMerchant(FakeFx("84.10"))
        app = create_app(self.merchant, tap.StaticDirectory({}), authority="testserver",
                         clock=lambda: datetime(2026, 11, 2, 10, 0, tzinfo=timezone.utc),
                         signer=receipt.MerchantSigner(Ed25519PrivateKey.generate(), "m-1"),
                         acp=AcpConfig(secret=SECRET, require_shopper_signature=True,
                                       public_url="https://shop.example"))
        self.client = TestClient(app)
        self.sku = next(iter(self.merchant.catalog))

    def post(self, path, body, key):
        raw = json.dumps(body).encode()
        return self.client.post(path, content=raw, headers={**hmac_auth.sign(raw, SECRET), "Idempotency-Key": key,
                                                            "content-type": "application/json"})

    def test_pending_approval_until_signed(self):
        s = self.post(SESSIONS, {"items": [{"id": self.sku, "quantity": 1}], "currency": "INR"}, "g-1").json()
        self.assertEqual(s["status"], "pending_approval")
        self.assertIn(f"https://shop.example/approve/{s['id']}", s["messages"][0]["content"])
        r = self.post(f"{SESSIONS}/{s['id']}/complete", {}, "g-2").json()
        self.assertEqual(r["status"], "pending_approval")
        self.assertEqual(r["messages"][0]["code"], "approval_required")
        self.assertEqual(self.merchant.orders, {})


@unittest.skipIf(TestClient is None, "needs the server extra")
class MinorUnitsTest(unittest.TestCase):
    def test_exact_conversion_only(self):
        self.assertEqual(to_minor(Decimal("12.34"), "USD"), 1234)
        self.assertEqual(to_minor(Decimal("1999"), "JPY"), 1999)
        with self.assertRaises(ValueError):
            to_minor(Decimal("12.345"), "USD")
        self.assertEqual(from_minor(1234, "USD"), Decimal("12.34"))
        for bad in (-1, 1.5, True, "12"):
            with self.assertRaises(ValueError):
                from_minor(bad, "USD")


if __name__ == "__main__":
    unittest.main()
