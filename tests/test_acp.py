"""V10 / C10: the ACP checkout API Airwallex calls, end to end through the edge app."""

import hashlib
import json
import re
import unittest
from datetime import datetime, timezone
from decimal import Decimal

from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from countersign import receipt, tap
from countersign.fx import FakeFx
from countersign.merchant import FakeMerchant

try:
    from fastapi.testclient import TestClient

    from countersign.edge import create_app
    from countersign.edge import hmac_auth
    from countersign.edge.acp import AcpConfig, from_minor, to_minor
    from countersign.edge.passkey import b64url
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

    def ceiling_for(self, factor):
        total = self.merchant.catalog[self.sku].price * Decimal("84.10")
        return to_minor((total * Decimal(factor)).quantize(Decimal("0.01")), "INR")

    def test_a_price_change_is_reported_once_as_the_total_not_also_as_the_items(self):
        s = self.create()
        self.merchant.set_price(self.sku, self.merchant.catalog[self.sku].price * 2)
        self.assertEqual([m["code"] for m in self.complete(s["id"]).json()["messages"]], ["price_change"])

    def test_a_requote_over_the_shoppers_ceiling_waits_for_a_new_ceiling(self):
        ceiling = self.ceiling_for("1.03")
        s = self.create(ceiling=ceiling)
        self.assertEqual(s["countersign"]["shopper_ceiling"], ceiling)
        self.merchant.set_price(self.sku, self.merchant.catalog[self.sku].price * Decimal("1.12"))
        r = self.complete(s["id"]).json()
        self.assertEqual(r["status"], "not_ready_for_payment")
        self.assertEqual([m["code"] for m in r["messages"]], ["price_change", "over_ceiling"])
        self.assertEqual(r["countersign"]["shopper_ceiling"], ceiling)  # never raised behind the shopper's back
        # Completing again is the agent's yes, not the shopper's: still nothing charged.
        again = self.complete(s["id"]).json()
        self.assertEqual((again["status"], [m["code"] for m in again["messages"]]),
                         ("not_ready_for_payment", ["over_ceiling"]))
        self.assertEqual(self.merchant.orders, {})
        new_total = next(t["amount"] for t in again["totals"] if t["type"] == "total")
        lifted = self.call("POST", f"{SESSIONS}/{s['id']}", {"ceiling": new_total}).json()
        self.assertEqual(lifted["status"], "ready_for_payment")
        self.assertEqual(self.complete(s["id"]).json()["status"], "completed")
        self.assertEqual(len(self.merchant.orders), 1)

    def test_a_currency_change_after_the_ceiling_holds_until_a_ceiling_in_the_new_currency(self):
        s = self.create(ceiling=self.ceiling_for("1.03"))
        # 865,000-odd paise must not become $8,650: the shopper's number was in rupees.
        moved = self.call("POST", f"{SESSIONS}/{s['id']}", {"currency": "usd"}).json()
        self.assertEqual((moved["status"], [m["code"] for m in moved["messages"]]),
                         ("not_ready_for_payment", ["over_ceiling"]))
        self.assertEqual(moved["messages"][0]["ceiling_currency"], "inr")
        self.assertEqual(moved["countersign"]["shopper_ceiling_currency"], "inr")
        self.merchant.set_price(self.sku, self.merchant.catalog[self.sku].price * Decimal("1.12"))
        for _ in range(2):  # the agent paying twice is still not the shopper's yes
            self.assertEqual(self.complete(s["id"]).json()["status"], "not_ready_for_payment")
        self.assertEqual(self.merchant.orders, {})
        dollars = self.call("POST", f"{SESSIONS}/{s['id']}", {"ceiling": 11000}).json()
        self.assertEqual((dollars["status"], dollars["countersign"]["shopper_ceiling_currency"]),
                         ("ready_for_payment", "usd"))
        self.assertEqual(self.complete(s["id"]).json()["status"], "completed")

    def test_a_ceiling_sent_with_a_currency_change_is_read_in_the_new_currency(self):
        s = self.create(ceiling=self.ceiling_for("1.03"))
        r = self.call("POST", f"{SESSIONS}/{s['id']}", {"currency": "usd", "ceiling": 900000}).json()
        self.assertEqual((r["status"], r["countersign"]["ceiling"]), ("ready_for_payment", 900000))
        # Read as paise, $9,000 would be ₹9,000, above the rupee price: it must hold anyway.
        back = self.call("POST", f"{SESSIONS}/{s['id']}", {"currency": "inr"}).json()
        self.assertEqual([m["code"] for m in back["messages"]], ["over_ceiling"])

    def test_a_ceiling_below_the_quoted_total_is_held_from_the_start(self):
        s = self.create(ceiling=self.ceiling_for("0.95"))
        self.assertEqual((s["status"], [m["code"] for m in s["messages"]]), ("not_ready_for_payment", ["over_ceiling"]))
        self.assertEqual(self.complete(s["id"]).json()["status"], "not_ready_for_payment")
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

    def test_an_expired_session_reads_as_canceled_on_the_wire(self):
        s = self.create()
        self.clock.now = self.clock.now.replace(hour=11, minute=1)  # past the one-hour session life
        r = self.call("GET", f"{SESSIONS}/{s['id']}").json()
        self.assertEqual(r["status"], "canceled")
        self.assertEqual(r["messages"][-1]["code"], "expired")
        done = self.complete(s["id"])
        self.assertEqual((done.status_code, done.json()["code"]), (405, "invalid_state"))
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

    def test_not_ready_for_payment_until_signed(self):
        s = self.post(SESSIONS, {"items": [{"id": self.sku, "quantity": 1}], "currency": "INR"}, "g-1").json()
        self.assertEqual(s["status"], "not_ready_for_payment")
        self.assertEqual(s["messages"][0]["code"], "approval_required")
        self.assertIn(f"https://shop.example/approve/{s['id']}", s["messages"][0]["content"])
        self.assertEqual(s["links"], [{"type": "approval", "url": f"https://shop.example/approve/{s['id']}"}])
        r = self.post(f"{SESSIONS}/{s['id']}/complete", {}, "g-2").json()
        self.assertEqual(r["status"], "not_ready_for_payment")
        self.assertEqual((r["messages"][0]["type"], r["messages"][0]["code"]), ("error", "approval_required"))
        self.assertEqual(r["links"][0]["type"], "approval")
        self.assertEqual(self.merchant.orders, {})

    def test_only_acp_statuses_reach_the_wire(self):
        acp_statuses = {"not_ready_for_payment", "ready_for_payment", "completed", "canceled"}
        s = self.post(SESSIONS, {"items": [{"id": self.sku, "quantity": 1}], "currency": "INR"}, "g-3").json()
        r = self.post(f"{SESSIONS}/{s['id']}/complete", {}, "g-4").json()
        c = self.post(f"{SESSIONS}/{s['id']}/cancel", {}, "g-5").json()
        self.assertLessEqual({s["status"], r["status"], c["status"]}, acp_statuses)
        self.assertEqual(c["links"], [])  # a canceled session has nothing left to approve


@unittest.skipIf(TestClient is None, "needs the server extra")
class ShopperPasskeyTest(unittest.TestCase):
    """U1 at the edge: the approval page's two calls, then complete. Assertions built as a browser builds them."""

    def setUp(self):
        self.clock = Clock()
        self.merchant = FakeMerchant(FakeFx("84.10"))
        self.app = create_app(self.merchant, tap.StaticDirectory({}), authority="testserver", clock=self.clock,
                              signer=receipt.MerchantSigner(Ed25519PrivateKey.generate(), "m-1"),
                              acp=AcpConfig(secret=SECRET, require_shopper_signature=True,
                                            public_url="https://shop.example"))
        self.client = TestClient(self.app)
        self.sku = next(iter(self.merchant.catalog))
        self.device = ec.generate_private_key(ec.SECP256R1())
        self.keys = 0

    def post(self, path, body):
        """Signed with the platform's HMAC, as Airwallex calls."""
        self.keys += 1
        raw = json.dumps(body).encode()
        return self.client.post(path, content=raw, headers={**hmac_auth.sign(raw, SECRET), "content-type": "application/json",
                                                            "Idempotency-Key": f"u1-{self.keys}"})

    def create(self):
        r = self.post(SESSIONS, {"items": [{"id": self.sku, "quantity": 1}], "currency": "INR"})
        self.assertEqual(r.status_code, 201, r.text)
        return r.json()

    def complete(self, sid):
        return self.post(f"{SESSIONS}/{sid}/complete", {}).json()

    def page(self, sid):
        r = self.client.get(f"/acp/approvals/{sid}")
        self.assertEqual(r.status_code, 200, r.text)
        return r.json()

    def assertion(self, challenge, *, key=None, origin="https://shop.example", credential=b"credential-1"):
        key = key or self.device
        authenticator_data = hashlib.sha256(b"shop.example").digest() + b"\x05" + (1).to_bytes(4, "big")
        client_data_json = json.dumps({"type": "webauthn.get", "challenge": challenge, "origin": origin}).encode()
        signature = key.sign(authenticator_data + hashlib.sha256(client_data_json).digest(), ec.ECDSA(hashes.SHA256()))
        public_key = key.public_key().public_bytes(serialization.Encoding.DER,
                                                   serialization.PublicFormat.SubjectPublicKeyInfo)
        return {"credential_id": b64url(credential), "public_key_spki_der": b64url(public_key),
                "authenticator_data": b64url(authenticator_data), "client_data_json": b64url(client_data_json),
                "signature": b64url(signature)}

    def sign(self, sid, **kw):
        return self.client.post(f"/acp/approvals/{sid}", json=self.assertion(self.page(sid)["webauthn"]["challenge"], **kw))

    def test_the_shopper_signs_then_the_charge_goes_through(self):
        s = self.create()
        self.assertEqual(s["status"], "not_ready_for_payment")
        page = self.page(s["id"])
        self.assertFalse(page["signed"])
        self.assertEqual(page["webauthn"]["rp_id"], "shop.example")
        self.assertEqual(page["webauthn"]["origin"], "https://shop.example")
        self.assertEqual(page["terms"]["terms_hash"], s["countersign"]["terms_hash"])
        self.assertEqual(page["terms"]["total"], s["totals"][-1]["amount"])
        self.assertEqual(page["terms"]["items"][0]["id"], self.sku)
        r = self.sign(s["id"])
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual((r.json()["status"], r.json()["signed"]), ("ready_for_payment", True))
        self.assertTrue(self.page(s["id"])["signed"])
        raw = b""
        now = self.client.get(f"{SESSIONS}/{s['id']}", headers=hmac_auth.sign(raw, SECRET)).json()
        self.assertEqual((now["status"], now["links"], now["messages"]), ("ready_for_payment", [], []))
        done = self.complete(s["id"])
        self.assertEqual(done["status"], "completed")
        self.assertEqual(len(self.merchant.orders), 1)
        kept = self.app.state.acp.sessions[s["id"]].shopper_assertion
        self.assertEqual(set(kept), {"credential_id", "public_key_spki_der", "authenticator_data", "client_data_json",
                                     "signature", "session_id", "rp_id", "origin"})
        self.assertEqual((kept["session_id"], kept["rp_id"], kept["origin"]),
                         (s["id"], "shop.example", "https://shop.example"))

    def test_a_failed_assertion_changes_nothing(self):
        s = self.create()
        r = self.sign(s["id"], origin="https://evil.example")
        self.assertEqual(r.status_code, 400)
        self.assertEqual((r.json()["code"], r.json()["message"]), ("assertion_invalid", "the origin does not match"))
        self.assertFalse(self.page(s["id"])["signed"])
        done = self.complete(s["id"])
        self.assertEqual(done["status"], "not_ready_for_payment")
        self.assertEqual(done["messages"][0]["code"], "approval_required")
        self.assertEqual(self.merchant.orders, {})

    def test_a_signature_for_one_session_cannot_approve_an_identical_one(self):
        a, b = self.create(), self.create()
        self.assertEqual(a["countersign"]["terms_hash"], b["countersign"]["terms_hash"])  # same cart, same terms
        replay = self.assertion(self.page(a["id"])["webauthn"]["challenge"])
        r = self.client.post(f"/acp/approvals/{b['id']}", json=replay)
        self.assertEqual((r.status_code, r.json()["message"]), (400, "the challenge is not for these terms"))
        self.assertEqual(self.complete(b["id"])["status"], "not_ready_for_payment")
        self.assertEqual(self.merchant.orders, {})

    def test_a_price_change_needs_a_new_signature(self):
        s = self.create()
        old = self.assertion(self.page(s["id"])["webauthn"]["challenge"])
        self.assertEqual(self.client.post(f"/acp/approvals/{s['id']}", json=old).status_code, 200)
        self.merchant.set_price(self.sku, self.merchant.catalog[self.sku].price * Decimal("1.05"))
        r = self.complete(s["id"])
        self.assertEqual(r["status"], "not_ready_for_payment")
        codes = [m["code"] for m in r["messages"]]
        self.assertIn("price_change", codes)
        self.assertEqual(codes[-1], "approval_required")  # the new terms wait for the shopper again
        self.assertEqual(self.merchant.orders, {})
        again = self.client.post(f"/acp/approvals/{s['id']}", json=old)
        self.assertEqual((again.status_code, again.json()["message"]), (400, "the challenge is not for these terms"))
        self.assertEqual(self.sign(s["id"]).status_code, 200)
        self.assertEqual(self.complete(s["id"])["status"], "completed")
        self.assertEqual(len(self.merchant.orders), 1)

    def test_over_the_ceiling_the_shoppers_passkey_on_the_new_terms_is_the_new_yes(self):
        total = self.merchant.catalog[self.sku].price * Decimal("84.10")
        r = self.post(SESSIONS, {"items": [{"id": self.sku, "quantity": 1}], "currency": "INR",
                                 "ceiling": to_minor((total * Decimal("1.03")).quantize(Decimal("0.01")), "INR")})
        s = r.json()
        self.assertEqual(self.sign(s["id"]).status_code, 200)
        self.merchant.set_price(self.sku, self.merchant.catalog[self.sku].price * Decimal("1.12"))
        self.assertEqual([m["code"] for m in self.complete(s["id"])["messages"]], ["price_change", "approval_required"])
        self.assertEqual(self.merchant.orders, {})
        self.assertEqual(self.sign(s["id"]).status_code, 200)  # they saw the new total on their own device
        self.assertEqual(self.complete(s["id"])["status"], "completed")
        self.assertEqual(len(self.merchant.orders), 1)

    def test_an_update_after_signing_clears_the_signature(self):
        s = self.create()
        self.assertEqual(self.sign(s["id"]).status_code, 200)
        r = self.post(f"{SESSIONS}/{s['id']}", {"items": [{"id": self.sku, "quantity": 1}]}).json()
        self.assertEqual(r["status"], "not_ready_for_payment")
        self.assertFalse(self.page(s["id"])["signed"])

    def test_a_credential_keeps_the_key_it_was_first_used_with(self):
        a, b = self.create(), self.create()
        self.assertEqual(self.sign(a["id"]).status_code, 200)
        r = self.sign(b["id"], key=ec.generate_private_key(ec.SECP256R1()))
        self.assertEqual((r.status_code, r.json()["message"]), (400, "this credential is registered with a different key"))
        self.assertEqual(self.sign(b["id"], key=ec.generate_private_key(ec.SECP256R1()), credential=b"other").status_code, 200)

    def test_the_approval_routes_need_no_hmac_but_the_checkout_api_still_does(self):
        s = self.create()
        self.assertEqual(self.client.get(f"{SESSIONS}/{s['id']}").status_code, 401)
        self.assertEqual(self.client.get(f"/acp/approvals/{s['id']}").status_code, 200)
        self.assertEqual(self.client.get("/acp/approvals/cs_missing").status_code, 404)

    def test_bad_bodies_are_refused(self):
        s = self.create()
        url = f"/acp/approvals/{s['id']}"
        r = self.client.post(url, json={})
        self.assertEqual((r.status_code, r.json()["param"]), (400, "$.credential_id"))
        self.assertEqual(self.client.post(url, content=b"not json").status_code, 400)
        self.assertEqual(self.client.post(url, json=[1]).status_code, 400)
        bad = {**self.assertion(self.page(s["id"])["webauthn"]["challenge"]), "signature": "not+base64url"}
        self.assertEqual(self.client.post(url, json=bad).json()["param"], "$.signature")

    def test_a_finished_session_cannot_be_signed(self):
        s = self.create()
        self.sign(s["id"])
        self.complete(s["id"])
        self.assertEqual(self.client.get(f"/acp/approvals/{s['id']}").status_code, 405)

    def test_without_a_public_url_there_is_no_origin_to_check(self):
        app = create_app(FakeMerchant(FakeFx("84.10")), tap.StaticDirectory({}), authority="testserver",
                         acp=AcpConfig(secret=SECRET))
        client = TestClient(app)
        raw = json.dumps({"items": [{"id": self.sku, "quantity": 1}], "currency": "INR"}).encode()
        s = client.post(SESSIONS, content=raw, headers={**hmac_auth.sign(raw, SECRET), "Idempotency-Key": "x",
                                                        "content-type": "application/json"}).json()
        r = client.get(f"/acp/approvals/{s['id']}")
        self.assertEqual((r.status_code, r.json()["code"]), (409, "invalid_state"))


@unittest.skipIf(TestClient is None, "needs the server extra")
class PaymentDataTest(unittest.TestCase):
    """A3: complete keeps what Airwallex sends as payment_data, never card data, and logs only that it came."""

    def setUp(self):
        self.merchant = FakeMerchant(FakeFx("84.10"))
        self.app = create_app(self.merchant, tap.StaticDirectory({}), authority="testserver",
                              clock=lambda: datetime(2026, 11, 2, 10, 0, tzinfo=timezone.utc),
                              signer=receipt.MerchantSigner(Ed25519PrivateKey.generate(), "m-1"),
                              acp=AcpConfig(secret=SECRET))
        self.client = TestClient(self.app)
        self.sku = next(iter(self.merchant.catalog))

    def post(self, path, body, key):
        raw = json.dumps(body).encode()
        return self.client.post(path, content=raw, headers={**hmac_auth.sign(raw, SECRET), "Idempotency-Key": key,
                                                            "content-type": "application/json"})

    def test_payment_data_is_kept_without_card_fields_and_never_logged(self):
        s = self.post(SESSIONS, {"items": [{"id": self.sku, "quantity": 1}], "currency": "INR"}, "p-1").json()
        payment = {"token": "tok_secret_value", "provider": "airwallex", "payment_intent_id": "int_123",
                   "card": {"number": "4035501000000008", "cvc": "737", "exp_month": "03", "brand": "visa"},
                   "note": "4035 5010 0000 0008"}
        with self.assertLogs("countersign.acp", level="WARNING") as logs:
            r = self.post(f"{SESSIONS}/{s['id']}/complete", {"payment_data": payment}, "p-2")
        self.assertEqual(r.json()["status"], "completed")
        kept = self.app.state.acp.sessions[s["id"]].payment_data
        self.assertEqual(kept, {"token": "tok_secret_value", "provider": "airwallex", "payment_intent_id": "int_123",
                                "card": {"brand": "visa"}, "note": "<removed>"})
        line = "\n".join(logs.output)
        self.assertIn("payment_data present, keys ['card', 'note', 'payment_intent_id', 'provider', 'token']", line)
        for value in ("4035501000000008", "4035 5010", "737", "tok_secret_value", "int_123"):
            # The session id, other ids and the signature are random and can contain "737" by chance.
            self.assertNotIn(value, re.sub(r"cs_[0-9a-f]{24}", "cs_<id>", line))
            self.assertNotIn(json.dumps(value), r.text)

    def test_no_payment_data_is_logged_as_absent(self):
        s = self.post(SESSIONS, {"items": [{"id": self.sku, "quantity": 1}], "currency": "INR"}, "p-3").json()
        with self.assertLogs("countersign.acp", level="WARNING") as logs:
            self.post(f"{SESSIONS}/{s['id']}/complete", {}, "p-4")
        self.assertIn("payment_data absent", "\n".join(logs.output))
        self.assertIsNone(self.app.state.acp.sessions[s["id"]].payment_data)


@unittest.skipIf(TestClient is None, "needs the server extra")
class ObserveModeTest(unittest.TestCase):
    """U4: a refused call teaches us the caller's contract without keeping any value."""

    def make(self, observe):
        app = create_app(FakeMerchant(FakeFx("84.10")), tap.StaticDirectory({}), authority="testserver",
                         acp=AcpConfig(secret=SECRET, observe=observe))
        return TestClient(app)

    def unknown_call(self, client):
        body = {"line_items": [{"sku": "AUD-018-0"}], "card_number": "4035501000000008", "address": "1 Secret Lane"}
        return client.post(SESSIONS, content=json.dumps(body).encode(),
                           headers={"X-Mystery-Signature": "value-must-not-be-logged", "Idempotency-Key": "o-1",
                                    "content-type": "application/json"})

    def test_names_and_keys_only(self):
        with self.assertLogs("countersign.acp", level="WARNING") as logs:
            r = self.unknown_call(self.make(observe=True))
        self.assertEqual(r.status_code, 401)
        line = "\n".join(logs.output)
        self.assertIn("x-mystery-signature", line)
        self.assertIn("idempotency-key", line)
        self.assertIn("'address', 'card_number', 'line_items'", line)
        for value in ("value-must-not-be-logged", "4035501000000008", "1 Secret Lane", "AUD-018-0", "o-1"):
            self.assertNotIn(value, line)

    def test_off_by_default(self):
        logger = __import__("logging").getLogger("countersign.acp")
        with self.assertNoLogs(logger, level="WARNING"):
            self.assertEqual(self.unknown_call(self.make(observe=False)).status_code, 401)


@unittest.skipIf(TestClient is None, "needs the server extra")
class FromEnvTest(unittest.TestCase):
    def test_no_secret_no_routes(self):
        self.assertIsNone(AcpConfig.from_env({}))
        app = create_app(FakeMerchant(FakeFx("84.10")), tap.StaticDirectory({}), authority="testserver",
                         acp=AcpConfig.from_env({}))
        raw = b"{}"
        r = TestClient(app).post(SESSIONS, content=raw, headers={**hmac_auth.sign(raw, SECRET), "Idempotency-Key": "x"})
        self.assertEqual(r.status_code, 401)  # still behind TAP: no ACP door at all
        self.assertIn("refused", r.json())

    def test_a_full_config(self):
        cfg = AcpConfig.from_env({"COUNTERSIGN_ACP_HMAC_SECRET": "x" * 32, "COUNTERSIGN_ACP_HMAC_SCHEME": "acp",
                                  "COUNTERSIGN_ACP_OBSERVE": "1", "COUNTERSIGN_REQUIRE_SHOPPER_SIGNATURE": "true",
                                  "COUNTERSIGN_ACP_CURRENCY": "inr", "COUNTERSIGN_PUBLIC_URL": "https://shop.example"})
        self.assertEqual((cfg.scheme, cfg.observe, cfg.require_shopper_signature, cfg.default_currency, cfg.public_url),
                         ("acp", True, True, "INR", "https://shop.example"))
        self.assertEqual(cfg.secret, b"x" * 32)

    def test_the_shopper_signature_needs_the_public_url(self):
        with self.assertRaises(ValueError):
            AcpConfig.from_env({"COUNTERSIGN_ACP_HMAC_SECRET": "x" * 32, "COUNTERSIGN_REQUIRE_SHOPPER_SIGNATURE": "1"})

    def test_origin_and_rp_id_come_from_the_public_url(self):
        cfg = AcpConfig(secret=b"x" * 32, public_url="https://Shop.Example:8443/base/")
        self.assertEqual((cfg.origin, cfg.rp_id), ("https://shop.example:8443", "shop.example"))
        self.assertEqual((AcpConfig(secret=b"x" * 32).origin, AcpConfig(secret=b"x" * 32).rp_id), (None, None))

    def test_a_short_secret_or_unknown_scheme_fails_loudly(self):
        with self.assertRaises(ValueError):
            AcpConfig.from_env({"COUNTERSIGN_ACP_HMAC_SECRET": "short"})
        with self.assertRaises(ValueError):
            AcpConfig.from_env({"COUNTERSIGN_ACP_HMAC_SECRET": "x" * 32, "COUNTERSIGN_ACP_HMAC_SCHEME": "any"})


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
