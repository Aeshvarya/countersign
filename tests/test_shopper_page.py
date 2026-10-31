"""The shopper's page: approve with a passkey (U1), pay in Airwallex's Drop-in, keep a receipt anyone can check."""

import base64
import hashlib
import json
import re
import unittest
from decimal import Decimal
from pathlib import Path
from urllib.parse import urlsplit

from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

from countersign import receipt, terms
from countersign.airwallex import AirwallexError
from countersign.airwallex_merchant import AirwallexMerchant
from countersign.fx import FakeFx

try:
    from tests.test_airwallex_merchant import FakeAirwallex
except ImportError:  # run by discover with -s tests
    from test_airwallex_merchant import FakeAirwallex

try:
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from countersign.edge import hmac_auth
    from countersign.edge.passkey import b64url
    from countersign.web import shopper
    from countersign.web.app import demo
    from countersign.web.host import Host, house_from_env
except ImportError:  # the server extra is not installed
    TestClient = None

STATIC = Path(__file__).resolve().parent.parent / "src" / "countersign" / "web" / "static"
PUBLIC = "https://countersign.onrender.com"
SECRET = "s" * 32
SESSIONS = "/acp/checkout_sessions"
ENV = {"COUNTERSIGN_PUBLIC_URL": PUBLIC, "COUNTERSIGN_ACP_HMAC_SECRET": SECRET,
       "COUNTERSIGN_MERCHANT_SIGNING_KEY": base64.b64encode(bytes(range(32))).decode()}


class ScriptTest(unittest.TestCase):
    def test_the_page_formats_amounts_with_the_servers_minor_units(self):
        # The routes send integer minor units; the page must divide by the same power of ten.
        source = (STATIC / "shopper.js").read_text()
        table = re.search(r"const MINOR_UNITS = (\{.*?\});", source)
        self.assertIsNotNone(table, "shopper.js lost its MINOR_UNITS table")
        self.assertEqual(json.loads(table.group(1)), terms._MINOR_UNITS)

    def test_the_browser_check_signs_the_same_bytes_as_the_merchant(self):
        # receipt.js rebuilds receipt._canonical; this pins the Python side it must match.
        self.assertEqual(receipt._canonical({"b": "₹ 5–7", "a": [1, None, True], "c": {"z": "\n", "y": "é"}}),
                         '{"a":[1,null,true],"b":"₹ 5–7","c":{"y":"é","z":"\\n"}}'.encode())


class Base(unittest.TestCase):
    u1 = True

    def merchant(self):
        return None  # the house default: pays instantly

    def setUp(self):
        env = {**ENV, **({"COUNTERSIGN_REQUIRE_SHOPPER_SIGNATURE": "1"} if self.u1 else {})}
        self.house = house_from_env(env, merchant=self.merchant())
        self.now = [100.0]
        self.shopper = shopper.Shopper(self.house, clock=lambda: self.now[0])
        app = FastAPI()
        app.include_router(shopper.router(self.house, self.shopper))
        self.client = TestClient(Host(app, self.house), base_url=PUBLIC)
        self.keys = 0
        self.device = ec.generate_private_key(ec.SECP256R1())

    def post(self, path, body):
        self.keys += 1
        raw = json.dumps(body).encode()
        r = self.client.post(path, content=raw, headers={**hmac_auth.sign(raw, SECRET.encode()),
                                                          "content-type": "application/json",
                                                          "Idempotency-Key": f"page-{self.keys}"})
        self.assertLess(r.status_code, 300, r.text)
        return r.json()

    def session(self, quantity=1):
        sku = self.house.merchant.search("kettle")[0].sku
        return self.post(SESSIONS, {"items": [{"id": sku, "quantity": quantity}], "currency": "INR"})

    def state(self, sid):
        r = self.client.get(f"/api/checkout/{sid}")
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(r.headers["cache-control"], "no-store")
        return r.json()

    def sign(self, sid):
        """A device signs the server's challenge bytes, as navigator.credentials.get() does."""
        data = self.client.get(f"/acp/approvals/{sid}").json()
        rp = urlsplit(PUBLIC).hostname.encode()
        authenticator_data = hashlib.sha256(rp).digest() + b"\x05" + (1).to_bytes(4, "big")
        client_data_json = json.dumps({"type": "webauthn.get", "challenge": data["webauthn"]["challenge"],
                                       "origin": data["webauthn"]["origin"], "crossOrigin": False}).encode()
        signature = self.device.sign(authenticator_data + hashlib.sha256(client_data_json).digest(),
                                     ec.ECDSA(hashes.SHA256()))
        spki = self.device.public_key().public_bytes(serialization.Encoding.DER,
                                                     serialization.PublicFormat.SubjectPublicKeyInfo)
        r = self.client.post(f"/acp/approvals/{sid}", json={
            "credential_id": b64url(b"device-1"), "public_key_spki_der": b64url(spki),
            "authenticator_data": b64url(authenticator_data), "client_data_json": b64url(client_data_json),
            "signature": b64url(signature)})
        self.assertEqual(r.status_code, 200, r.text)

    def verifies(self, state):
        key = Ed25519PublicKey.from_public_bytes(base64.b64decode(state["merchant_key"]["public_key"]))
        return receipt.verify(state["receipt"], key).ok


@unittest.skipIf(TestClient is None, "needs the server extra: pip install -e '.[server]' httpx")
class ShopperStagesTest(Base):
    def test_approve_then_wait_then_paid_with_a_receipt_the_page_can_check(self):
        s = self.session()
        st = self.state(s["id"])
        self.assertEqual((st["stage"], st["signed"], st["approval_required"]), ("approve", False, True))
        self.assertEqual(st["terms"]["items"][0]["quantity"], 1)
        self.assertEqual(st["terms"]["total"], s["totals"][-1]["amount"])
        self.assertIsNone(st["order"])

        self.sign(s["id"])
        st = self.state(s["id"])
        self.assertEqual((st["stage"], st["signed"]), ("waiting", True))

        self.post(f"{SESSIONS}/{s['id']}/complete", {})
        st = self.state(s["id"])
        self.assertEqual((st["stage"], st["order"]["status"]), ("paid", "paid"))
        self.assertIsNone(st["payment"])
        self.assertTrue(self.verifies(st))

    def test_a_requote_sends_the_shopper_back_to_approve(self):
        s = self.session()
        self.sign(s["id"])
        sku = self.house.merchant.search("kettle")[0].sku
        self.post(f"{SESSIONS}/{s['id']}", {"items": [{"id": sku, "quantity": 2}]})
        st = self.state(s["id"])
        self.assertEqual((st["stage"], st["signed"]), ("approve", False))
        self.assertEqual(st["terms"]["items"][0]["quantity"], 2)

    def test_a_canceled_checkout_is_closed_and_nothing_is_charged(self):
        s = self.session()
        self.post(f"{SESSIONS}/{s['id']}/cancel", {})
        st = self.state(s["id"])
        self.assertEqual((st["stage"], st["order"]), ("canceled", None))

    def test_an_unknown_checkout_is_not_found(self):
        r = self.client.get("/api/checkout/cs_nope")
        self.assertEqual(r.status_code, 404)
        self.assertEqual(self.client.post("/api/checkout/cs_nope/confirm").status_code, 404)

    def test_confirm_with_nothing_to_check_just_answers(self):
        s = self.session()
        r = self.client.post(f"/api/checkout/{s['id']}/confirm")
        self.assertEqual((r.status_code, r.json()["stage"]), (200, "approve"))


@unittest.skipIf(TestClient is None, "needs the server extra: pip install -e '.[server]' httpx")
class ShopperWithoutU1Test(Base):
    u1 = False

    def test_no_passkey_step_when_u1_is_off(self):
        st = self.state(self.session()["id"])
        self.assertEqual((st["stage"], st["approval_required"]), ("waiting", False))


@unittest.skipIf(TestClient is None, "needs the server extra: pip install -e '.[server]' httpx")
class ShopperCardTest(Base):
    """COUNTERSIGN_MERCHANT=airwallex: the order waits for the card, paid only from Airwallex's own record."""

    def merchant(self):
        self.api = FakeAirwallex()
        return AirwallexMerchant(self.api, FakeFx("84.10"))

    def completed(self):
        s = self.session()
        self.sign(s["id"])
        self.post(f"{SESSIONS}/{s['id']}/complete", {})
        key = f"acp:{s['id']}"
        return s["id"], self.house.merchant.orders[key], self.house.merchant._decisions[key][0]

    def test_the_page_gets_what_drop_in_needs_and_nothing_else(self):
        sid, order, decision = self.completed()
        st = self.state(sid)
        self.assertEqual(st["stage"], "pay")
        self.assertEqual(st["payment"], {"intent_id": order.payment_id, "client_secret": "cs_1", "currency": "USD",
                                         "quote_id": decision.fx_quote_id, "env": "demo"})
        self.assertEqual((st["order"]["status"], st["order"]["currency"]), ("pending", "INR"))
        self.assertIsNone(st["receipt"])

    def test_paid_only_once_airwallex_says_so_then_the_receipt_is_signed(self):
        sid, order, decision = self.completed()
        early = self.client.post(f"/api/checkout/{sid}/confirm").json()  # Drop-in said success too soon
        self.assertEqual(early["stage"], "pay")

        self.api.shopper_pays(order.payment_id, decision.charge, quote=decision.fx_quote_id)
        self.now[0] += shopper.CONFIRM_EVERY
        st = self.client.post(f"/api/checkout/{sid}/confirm").json()
        self.assertEqual((st["stage"], st["order"]["status"]), ("paid", "paid"))
        self.assertIsNone(st["payment"])
        self.assertTrue(self.verifies(st))
        self.assertEqual(self.state(sid)["receipt"], st["receipt"])

    def test_confirm_is_throttled_per_checkout(self):
        sid, _, _ = self.completed()
        self.assertEqual(self.client.post(f"/api/checkout/{sid}/confirm").status_code, 200)
        r = self.client.post(f"/api/checkout/{sid}/confirm")
        self.assertEqual((r.status_code, r.json()["code"]), (429, "too_soon"))

    def test_a_payment_that_does_not_match_is_refunded_and_the_page_says_so(self):
        sid, order, decision = self.completed()
        self.api.shopper_pays(order.payment_id, decision.charge + Decimal("50"), quote=decision.fx_quote_id)
        st = self.client.post(f"/api/checkout/{sid}/confirm").json()
        self.assertEqual((st["stage"], st["order"]["status"]), ("failed", "refunded"))
        self.assertTrue(any("countersign allowed" in p for p in st["problems"]))
        self.assertIsNone(st["receipt"])
        self.assertEqual(len(self.api.refunds), 1)

    def test_airwallex_unreachable_leaves_it_to_the_webhook(self):
        sid, _, _ = self.completed()

        def down(path, params=None):
            raise AirwallexError(503, "unavailable")

        self.api.get = down
        r = self.client.post(f"/api/checkout/{sid}/confirm")
        self.assertEqual((r.status_code, r.json()["code"]), (503, "airwallex_unreachable"))
        self.assertEqual(self.state(sid)["stage"], "pay")


@unittest.skipIf(TestClient is None, "needs the server extra: pip install -e '.[server]' httpx")
class PagesTest(unittest.TestCase):
    def setUp(self):
        self.client = TestClient(demo, base_url=PUBLIC)

    def test_approve_and_pay_links_open_the_same_page(self):
        for path in ("/approve/cs_anything", "/pay/cs_anything"):
            page = self.client.get(path)
            self.assertEqual(page.status_code, 200)
            self.assertIn("text/html", page.headers["content-type"])
            self.assertIn('<script src="/static/shopper.js"></script>', page.text)
        for name in ("shopper.js", "receipt.js", "verify.js"):
            script = self.client.get(f"/static/{name}")
            self.assertEqual(script.status_code, 200, name)
            self.assertIn("javascript", script.headers["content-type"])

    def test_only_listed_scripts_are_served(self):
        for name in ("app.py", "..%2Fapp.py", "index.html", "console.js.map"):
            self.assertEqual(self.client.get(f"/static/{name}").status_code, 404, name)

    def test_the_shopper_page_cannot_be_framed_or_leak_its_address(self):
        page = self.client.get("/approve/cs_anything")
        csp = page.headers["content-security-policy"]
        self.assertIn("frame-ancestors 'none'", csp)
        self.assertIn("default-src 'none'", csp)
        self.assertIn("frame-src https://*.airwallex.com", csp)  # Drop-in, and nothing else
        self.assertEqual(page.headers["referrer-policy"], "strict-origin")
        self.assertEqual(page.headers["x-frame-options"], "DENY")
        self.assertEqual(page.headers["cache-control"], "no-store")
        self.assertNotRegex(page.text, r"(src|href)=\"(https?:)?//")

    def test_the_verify_page_talks_to_nobody_but_this_site(self):
        page = self.client.get("/verify")
        self.assertEqual(page.status_code, 200)
        csp = page.headers["content-security-policy"]
        self.assertNotIn("airwallex", csp)
        self.assertIn("connect-src 'self'", csp)
        self.assertEqual(page.headers["referrer-policy"], "no-referrer")


if __name__ == "__main__":
    unittest.main()
