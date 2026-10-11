import unittest
from datetime import datetime, timezone
from decimal import Decimal

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from countersign import tap, wire
from countersign.fx import FakeFx
from countersign.merchant import FakeMerchant

try:
    from fastapi.testclient import TestClient

    from countersign.edge import create_app
except ImportError:  # the server extra is not installed
    TestClient = None

AGENT = "https://agents.example"
AUTHORITY = "testserver"  # the host TestClient sends to


class Clock:
    def __init__(self):
        self.now = datetime(2026, 11, 2, 10, 0, tzinfo=timezone.utc)

    def __call__(self):
        return self.now


@unittest.skipIf(TestClient is None, "needs the server extra: pip install -e '.[server]' httpx")
class EdgeTest(unittest.TestCase):
    def setUp(self):
        self.key = Ed25519PrivateKey.generate()
        self.clock = Clock()
        self.fx = FakeFx("84.10")
        self.merchant = FakeMerchant(self.fx)
        directory = tap.StaticDirectory({(AGENT, "k1"): self.key.public_key()})
        self.app = create_app(self.merchant, directory, authority=AUTHORITY, clock=self.clock)
        self.client = TestClient(self.app)

    def sign(self, path, tag=tap.BROWSE, key=None, authority=AUTHORITY):
        return tap.sign(key or self.key, authority=authority, path=path, key_id="k1",
                        signature_agent=AGENT, tag=tag).headers()

    def get(self, path, tag=tap.BROWSE, headers=None):
        return self.client.get(path, headers=self.sign(path, tag) if headers is None else headers)


class PairTwoOverHttpTest(EdgeTest):
    """Same request, four callers: only the signed agent gets an answer."""

    path = "/products?q=keyboard&limit=3"

    def test_a_signed_agent_is_served(self):
        r = self.get(self.path)
        self.assertEqual(r.status_code, 200)
        self.assertTrue(r.json())
        self.assertTrue(all("keyboard" in p["title"].lower() for p in r.json()))

    def test_an_unsigned_caller_is_refused(self):
        r = self.get(self.path, headers={})
        self.assertEqual(r.status_code, 401)
        self.assertEqual(set(r.json()), {"refused"})
        self.assertIn("unsigned", r.json()["refused"])

    def test_a_replayed_signature_is_refused(self):
        headers = self.sign(self.path)
        self.assertEqual(self.get(self.path, headers=headers).status_code, 200)
        r = self.get(self.path, headers=headers)
        self.assertEqual(r.status_code, 401)
        self.assertIn("already been used", r.json()["refused"])

    def test_a_signature_for_another_page_is_refused(self):
        r = self.get(self.path, headers=self.sign("/products?q=kettle&limit=3"))
        self.assertEqual(r.status_code, 401)
        self.assertIn("does not match", r.json()["refused"])


class EdgeCheckTest(EdgeTest):
    def test_a_signature_for_another_merchant_is_refused_whatever_host_is_claimed(self):
        path = "/products?q=kettle"
        headers = {**self.sign(path, authority="other.example"), "Host": "other.example"}
        r = self.client.get(path, headers=headers)
        self.assertEqual(r.status_code, 401)
        self.assertIn("does not match", r.json()["refused"])

    def test_a_stranger_key_under_a_known_id_is_refused(self):
        r = self.get("/products?q=kettle", headers=self.sign("/products?q=kettle", key=Ed25519PrivateKey.generate()))
        self.assertEqual(r.status_code, 401)

    def test_a_payer_signature_cannot_browse(self):
        r = self.get("/products?q=kettle", tag=tap.PAY)
        self.assertEqual(r.status_code, 401)
        self.assertIn("needs agent-browser-auth", r.json()["refused"])

    def test_unknown_routes_are_behind_the_check_too(self):
        self.assertEqual(self.client.get("/admin").status_code, 401)
        self.assertEqual(self.get("/admin").status_code, 404)

    def test_no_open_docs(self):
        self.assertEqual(self.get("/docs").status_code, 404)
        self.assertEqual(self.get("/openapi.json").status_code, 404)

    def test_every_check_is_logged(self):
        self.get("/products?q=kettle")
        self.get("/products?q=kettle", headers={})
        log = self.app.state.edge_log
        self.assertEqual([e["ok"] for e in log], [True, False])
        self.assertEqual(log[0]["detail"], "k1")
        self.assertEqual(log[0]["tag"], tap.BROWSE)


class OffersTest(EdgeTest):
    def post(self, path, body, tag=tap.BROWSE, headers=None):
        return self.client.post(path, json=body, headers=self.sign(path, tag) if headers is None else headers)

    def test_an_offer_carries_a_rate_lock_and_round_trips_as_terms(self):
        sku = self.merchant.search("kettle")[0].sku
        r = self.post("/offers", {"cart": {sku: 2}, "shopper_currency": "INR"})
        self.assertEqual(r.status_code, 200)
        terms = wire.terms_from_json(r.json())
        self.assertEqual(terms.items[0].quantity, 2)
        self.assertEqual(terms.fx.rate, Decimal("84.10"))
        self.assertEqual(terms.shopper_total, terms.computed_shopper_total())

    def test_offers_need_a_browse_signature(self):
        sku = self.merchant.search("kettle")[0].sku
        self.assertEqual(self.post("/offers", {"cart": {sku: 1}, "shopper_currency": "INR"}, headers={}).status_code, 401)

    def test_an_unknown_sku_is_not_found(self):
        r = self.post("/offers", {"cart": {"NOPE-1": 1}, "shopper_currency": "INR"})
        self.assertEqual(r.status_code, 404)
        self.assertIn("NOPE-1", r.json()["error"])

    def test_more_than_the_stock_is_a_conflict(self):
        product = self.merchant.search("kettle")[0]
        r = self.post("/offers", {"cart": {product.sku: product.stock + 1}, "shopper_currency": "INR"})
        self.assertEqual(r.status_code, 409)

    def test_broken_carts_are_rejected(self):
        sku = self.merchant.search("kettle")[0].sku
        self.assertEqual(self.post("/offers", {"cart": {}, "shopper_currency": "INR"}).status_code, 422)
        self.assertEqual(self.post("/offers", {"cart": {sku: 0}, "shopper_currency": "INR"}).status_code, 400)
        self.assertEqual(self.post("/offers", {"cart": {sku: 1}, "shopper_currency": "rupees"}).status_code, 422)


if __name__ == "__main__":
    unittest.main()
