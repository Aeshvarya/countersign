import threading
import time
import unittest
from datetime import datetime, timedelta, timezone
from decimal import Decimal

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from countersign import receipt, tap, wire
from countersign.fx import FakeFx
from countersign.merchant import FakeMerchant
from countersign.terms import Approval

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
    merchant_class = FakeMerchant

    def setUp(self):
        self.key = Ed25519PrivateKey.generate()
        self.clock = Clock()
        self.fx = FakeFx("84.10")
        self.merchant = self.merchant_class(self.fx)
        self.other_key = Ed25519PrivateKey.generate()  # a second registered agent
        directory = tap.StaticDirectory({(AGENT, "k1"): self.key.public_key(), (AGENT, "k2"): self.other_key.public_key()})
        self.merchant_key = Ed25519PrivateKey.generate()
        self.app = create_app(self.merchant, directory, authority=AUTHORITY, clock=self.clock,
                              signer=receipt.MerchantSigner(self.merchant_key, "m-1"))
        self.client = TestClient(self.app)

    def sign(self, path, tag=tap.BROWSE, key=None, authority=AUTHORITY, key_id="k1"):
        return tap.sign(key or self.key, authority=authority, path=path, key_id=key_id,
                        signature_agent=AGENT, tag=tag).headers()

    def get(self, path, tag=tap.BROWSE, headers=None):
        return self.client.get(path, headers=self.sign(path, tag) if headers is None else headers)

    def post(self, path, body, tag=tap.BROWSE, headers=None):
        return self.client.post(path, json=body, headers=self.sign(path, tag) if headers is None else headers)


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


class SlowMerchant(FakeMerchant):
    """Rebuilding the offer takes a while, as it will on a real merchant; counts charge attempts."""

    pays = 0

    def offer(self, *args, **kwargs):
        time.sleep(0.2)
        return super().offer(*args, **kwargs)

    def pay(self, terms, decision, idempotency_key, approval_hash=None):
        self.pays += 1
        return super().pay(terms, decision, idempotency_key, approval_hash=approval_hash)


class CheckoutBase(EdgeTest):
    def setUp(self):
        super().setUp()
        self.sku = self.merchant.search("mechanical keyboard")[0].sku
        terms = wire.terms_from_json(self.post("/offers", {"cart": {self.sku: 1}, "shopper_currency": "INR"}).json())
        now = self.clock()
        self.approval = Approval.grant(terms, terms.shopper_total * Decimal("1.02"), "shopper@example.com",
                                       now, now + timedelta(minutes=30))

    def checkout(self, approval=None, key="order-1", tag=tap.PAY, headers=None):
        body = {"approval": approval if isinstance(approval, dict) else wire.approval_to_json(approval or self.approval),
                "idempotency_key": key}
        return self.post("/checkout", body, tag=tag, headers=headers)


class CheckoutTest(CheckoutBase):
    def test_an_approved_checkout_is_charged_with_an_order_number_and_a_receipt(self):
        r = self.checkout()
        self.assertEqual(r.status_code, 200)
        out = r.json()
        self.assertEqual(out["decision"]["outcome"], "proceed")
        self.assertEqual(out["order"]["order_number"], "ORD-1001")
        self.assertEqual(out["order"]["currency"], "INR")
        self.assertTrue(receipt.verify(out["receipt"], self.merchant_key.public_key(), self.approval).ok)
        self.assertEqual(out["receipt"]["body"]["agent"]["key_id"], "k1")
        self.assertEqual(out["receipt"]["body"]["agent"]["tag"], tap.PAY)

    def test_paying_needs_a_payer_signature(self):
        self.assertEqual(self.checkout(headers={}).status_code, 401)
        r = self.checkout(tag=tap.BROWSE)
        self.assertEqual(r.status_code, 401)
        self.assertIn("needs agent-payer-auth", r.json()["refused"])
        self.assertEqual(self.merchant.orders, {})

    def test_a_rate_past_the_ceiling_asks_again_and_charges_nothing(self):
        self.fx.rate = Decimal("88.40")
        out = self.checkout().json()
        self.assertEqual(out["decision"]["outcome"], "reask")
        self.assertEqual(out["decision"]["changes"][0]["field"], "shopper_total")
        self.assertIsNone(out["order"])
        self.assertEqual(self.merchant.orders, {})

    def test_a_retry_returns_the_same_order_and_receipt_and_never_charges_twice(self):
        first = self.checkout().json()
        second = self.checkout().json()
        self.assertEqual(second["order"], first["order"])
        self.assertEqual(second["receipt"], first["receipt"])
        self.assertEqual(len(self.merchant.orders), 1)

    def test_an_approval_edited_in_transit_is_refused(self):
        data = wire.approval_to_json(self.approval)
        data["ceiling"] = "999999.00"
        out = self.checkout(data).json()
        self.assertEqual(out["decision"]["outcome"], "refuse")
        self.assertEqual(self.merchant.orders, {})

    def test_an_unreadable_approval_is_refused_not_a_server_error(self):
        broken = [
            {},
            {**wire.approval_to_json(self.approval), "ceiling": "lots"},
            {**wire.approval_to_json(self.approval), "ceiling": "Infinity"},
            {**wire.approval_to_json(self.approval), "expires_at": "2026-11-02T10:30:00"},  # no timezone
            {**wire.approval_to_json(self.approval), "terms": {**wire.terms_to_json(self.approval.terms), "items": []}},
        ]
        for i, data in enumerate(broken):
            with self.subTest(i):
                r = self.checkout(data, key=f"broken-{i}")
                self.assertEqual(r.status_code, 200)
                self.assertEqual(r.json()["decision"]["outcome"], "refuse")
        self.assertEqual(self.merchant.orders, {})

    def test_an_idempotency_key_must_be_a_plain_token(self):
        self.assertEqual(self.checkout(key="../../etc").status_code, 422)
        self.assertEqual(self.checkout(key="").status_code, 422)

    def test_an_order_can_be_looked_up_by_its_key_with_a_payer_signature(self):
        order = self.checkout().json()["order"]
        self.assertEqual(self.get("/orders/order-1", tag=tap.PAY).json(), order)
        self.assertEqual(self.get("/orders/order-1", tag=tap.BROWSE).status_code, 401)
        self.assertEqual(self.get("/orders/order-9", tag=tap.PAY).status_code, 404)

    def test_another_agent_cannot_see_or_take_over_an_order(self):
        mine = self.checkout().json()
        as_other = lambda path: self.sign(path, tap.PAY, key=self.other_key, key_id="k2")
        self.assertEqual(self.get("/orders/order-1", headers=as_other("/orders/order-1")).status_code, 404)
        theirs = self.checkout(headers=as_other("/checkout")).json()
        self.assertEqual(theirs["decision"]["outcome"], "refuse")
        self.assertIsNone(theirs["order"])
        self.assertIsNone(theirs["receipt"])
        self.assertEqual(self.get("/orders/order-1", tag=tap.PAY).json(), mine["order"])
        self.assertEqual(len(self.merchant.orders), 1)


class ConcurrentRetryTest(CheckoutBase):
    merchant_class = SlowMerchant

    def test_two_checkouts_with_one_key_at_the_same_moment_charge_once(self):
        answers = []
        threads = [threading.Thread(target=lambda: answers.append(self.checkout().json())) for _ in range(2)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        self.assertEqual(self.merchant.pays, 1)
        self.assertEqual(answers[0]["order"], answers[1]["order"])
        self.assertEqual(answers[0]["receipt"], answers[1]["receipt"])


if __name__ == "__main__":
    unittest.main()
