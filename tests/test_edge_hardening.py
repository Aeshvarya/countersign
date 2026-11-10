"""Odd inputs at the merchant's edge: each is refused with a reason, never charged, never a 500.

Found by a bug hunt on 2026-11-10 (quantities, header digits, nesting, out-of-range ceilings,
bodies on GET, dispute totals with shipping). Each test failed before its fix.
"""
import json
import unittest
from datetime import timedelta
from decimal import Decimal

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from countersign import receipt, tap, wire
from countersign.fx import FakeFx
from countersign.merchant import FakeMerchant
from countersign.terms import Approval, LineItem, Terms, money, terms_hash

try:
    from fastapi.testclient import TestClient
    from countersign.edge import create_app, hmac_auth
    from countersign.edge.acp import AcpConfig, to_minor
    from countersign.edge.app import body_refusal
    from countersign.edge.passkey import verify_assertion
except ImportError:  # the server extra is not installed
    TestClient = None

from test_edge import AGENT, AUTHORITY, Clock

SECRET = b"k" * 32
DEEP = b"[" * 30000 + b"]" * 30000  # 60,000 bytes: under the body cap, past json's nesting limit


@unittest.skipIf(TestClient is None, "needs the server extra")
class EdgeHardeningTest(unittest.TestCase):
    def setUp(self):
        self.key = Ed25519PrivateKey.generate()
        self.clock = Clock()
        self.merchant = FakeMerchant(FakeFx("84.10"))
        self.config = AcpConfig(secret=SECRET)
        self.app = create_app(self.merchant, tap.StaticDirectory({(AGENT, "k1"): self.key.public_key()}),
                              authority=AUTHORITY, clock=self.clock,
                              signer=receipt.MerchantSigner(Ed25519PrivateKey.generate(), "m-1"), acp=self.config)
        self.client = TestClient(self.app, raise_server_exceptions=False)
        self.sku = next(iter(self.merchant.catalog))

    # -- the TAP door

    def tap_checkout(self, quantities):
        items, total = [], Decimal("0.00")
        for sku, qty in quantities:
            p = self.merchant.catalog[sku]
            items.append(LineItem(sku, p.title, qty, p.price))
            total += p.price * qty
        total = money(total, "USD")
        terms = Terms(self.merchant.merchant_id, tuple(items), "standard", "USD", total, "USD", total)
        now = self.clock()
        approval = Approval(terms, total, now, now + timedelta(minutes=30), "agent", terms_hash(terms, total))
        data = json.dumps({"approval": wire.approval_to_json(approval), "idempotency_key": "q-1"}).encode()
        headers = tap.sign(self.key, authority=AUTHORITY, path="/checkout", key_id="k1",
                           signature_agent=AGENT, tag=tap.PAY, body=data).headers()
        return self.client.post("/checkout", content=data, headers={**headers, "Content-Type": "application/json"})

    def test_the_tap_door_refuses_a_quantity_below_one(self):
        dear, cheap = sorted(self.merchant.catalog, key=lambda s: self.merchant.catalog[s].price)[-1:-3:-1]
        for cart in ([(dear, -1)], [(dear, 0)], [(dear, 1), (cheap, -1)]):
            out = self.tap_checkout(cart).json()
            self.assertEqual(out["decision"]["outcome"], "refuse", cart)
            self.assertIsNone(out.get("receipt"), cart)
            self.assertIsNone(out.get("order"), cart)

    def test_a_tap_created_time_of_4301_digits_is_a_401(self):
        headers = {"Signature-Agent": f'"{AGENT}"', "Signature": "sig2=:AAAA:",
                   "Signature-Input": 'sig2=("@authority" "@path"); created=' + "9" * 4301
                   + '; expires=1; keyId="k1"; alg="ed25519"; nonce="n"; tag="agent-browser-auth"'}
        self.assertEqual(self.client.get("/products", headers=headers).status_code, 401)

    # -- the ACP door

    def test_an_hmac_timestamp_of_4301_digits_is_a_401(self):
        r = self.client.post("/acp/create_checkout", content=b"{}",
                             headers={"x-timestamp": "9" * 4301, "x-signature": "00"})
        self.assertEqual(r.status_code, 401, r.text)

    def test_a_content_length_of_4301_digits_is_a_413(self):
        self.assertEqual(body_refusal("9" * 4301)[0], 413)

    def test_a_get_with_a_body_on_the_acp_routes_is_capped(self):
        r = self.client.request("GET", "/acp/approvals/cs_x", content=b" " * (1024 * 1024))
        self.assertEqual(r.status_code, 413, r.text)

    def test_deep_json_is_a_400_on_the_approval_page_and_the_sessions(self):
        r = self.client.post("/acp/approvals/cs_x", content=DEEP, headers={"content-type": "application/json"})
        self.assertEqual(r.status_code, 400, r.text)
        h = {"content-type": "application/json", "Idempotency-Key": "deep-1",
             **hmac_auth.sign(DEEP, SECRET, scheme=self.config.scheme)}
        r = self.client.post("/acp/checkout_sessions", content=DEEP, headers=h)
        self.assertEqual(r.status_code, 400, r.text)

    def test_deep_client_data_json_is_refused_by_the_passkey_check(self):
        why = verify_assertion(public_key_spki_der=b"", client_data_json=DEEP, authenticator_data=b"",
                               signature=b"", expected_challenge=b"c", origin="https://testserver",
                               rp_id="testserver")
        self.assertIn("not valid JSON", why)

    def test_an_out_of_range_ceiling_is_a_400(self):
        body = {"items": [{"id": self.sku, "quantity": 1}], "currency": "USD", "ceiling": 10 ** 28}
        raw = json.dumps(body).encode()
        h = {"content-type": "application/json", "Idempotency-Key": "c-1",
             **hmac_auth.sign(raw, SECRET, scheme=self.config.scheme)}
        r = self.client.post("/acp/checkout_sessions", content=raw, headers=h)
        self.assertEqual(r.status_code, 400, r.text)

    def test_an_infinite_amount_is_a_value_error(self):
        with self.assertRaises(ValueError):
            to_minor(Decimal("Infinity"), "USD")

    def test_a_currency_that_upper_cases_to_four_letters_is_refused(self):
        raw = json.dumps({"items": [{"id": self.sku, "quantity": 1}], "currency": "ßab"}).encode()
        h = {"content-type": "application/json", "Idempotency-Key": "cur-1",
             **hmac_auth.sign(raw, SECRET, scheme=self.config.scheme)}
        r = self.client.post("/acp/checkout_sessions", content=raw, headers=h)
        self.assertEqual(r.status_code, 400, r.text)


if __name__ == "__main__":
    unittest.main()
