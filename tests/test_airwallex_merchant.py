"""V5′: AirwallexMerchant over a fake Airwallex, end to end through the countersign (C9)."""

import unittest
from datetime import datetime, timedelta, timezone
from decimal import Decimal

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from countersign import receipt as receipts
from countersign.airwallex_merchant import AirwallexMerchant, intent_of
from countersign.checkout import MemoryReceiptStore, countersign, release
from countersign.decide import Outcome
from countersign.fx import FakeFx
from countersign.terms import Approval

NOW = datetime(2026, 11, 2, 10, 0, tzinfo=timezone.utc)


class FakeAirwallex:
    """The payment intent and refund APIs as the sandbox answers them (shapes from the Airwallex docs)."""

    def __init__(self):
        self.intents = {}
        self.refunds = []
        self.creates = 0

    def request_id(self):
        return "req"

    def post(self, path, body=None, extra_headers=None):
        if path == "/pa/payment_intents/create":
            self.creates += 1
            for intent in self.intents.values():
                if intent["request_id"] == body["request_id"]:
                    return intent
            intent = {"id": f"int_{len(self.intents) + 1}", "request_id": body["request_id"],
                      "amount": body["amount"], "currency": body["currency"], "status": "REQUIRES_PAYMENT_METHOD",
                      "merchant_order_id": body["merchant_order_id"], "metadata": body["metadata"],
                      "client_secret": f"cs_{len(self.intents) + 1}"}
            self.intents[intent["id"]] = intent
            return intent
        if path == "/pa/refunds/create":
            if not any(r["request_id"] == body["request_id"] for r in self.refunds):
                self.refunds.append(body)
            return {"id": "rfd_1", "status": "RECEIVED"}
        raise AssertionError(path)

    def get(self, path, params=None):
        return dict(self.intents[path.rsplit("/", 1)[1]])

    def shopper_pays(self, intent_id, amount, currency="INR", quote="cvq_fake_1", status="SUCCEEDED"):
        """What Drop-in leaves behind: the intent reads back in the shopper's currency."""
        self.intents[intent_id].update(status=status, amount=float(amount), currency=currency,
                                       conversion_quote_id=quote)


class AirwallexMerchantTest(unittest.TestCase):
    def setUp(self):
        self.api = FakeAirwallex()
        self.shop = AirwallexMerchant(self.api, FakeFx("84.10"))
        self.sku = self.shop.search("kettle")[0].sku
        self.stock = self.shop.catalog[self.sku].stock
        terms = self.shop.offer({self.sku: 1}, "INR", NOW)
        self.approval = Approval.grant(terms, terms.shopper_total, "s", NOW, NOW + timedelta(hours=2))
        self.key = Ed25519PrivateKey.generate()
        self.signer = receipts.MerchantSigner(self.key, "m-1")
        self.store = MemoryReceiptStore()

    def checkout(self, key="k1"):
        return countersign(self.shop, self.approval, key, NOW, signer=self.signer, store=self.store)

    def test_proceed_starts_one_intent_and_hands_drop_in_the_rate_lock(self):
        r = self.checkout()
        self.assertEqual((r.order.status, r.receipt), ("pending", None))
        intent = self.api.intents[r.order.payment_id]
        self.assertEqual(intent["currency"], "USD")
        self.assertEqual(intent["merchant_order_id"], r.order.order_number)
        self.assertEqual(intent["metadata"]["approval_hash"], self.approval.hash)
        self.assertEqual(intent["request_id"], "k1")
        handoff = self.shop.handoff("k1")
        self.assertEqual((handoff.client_secret, handoff.quote_id), (intent["client_secret"], r.decision.fx_quote_id))
        self.assertEqual(self.shop.catalog[self.sku].stock, self.stock - 1)

    def test_a_retry_while_the_shopper_is_in_drop_in_starts_no_second_intent(self):
        self.checkout()
        self.checkout()
        self.assertEqual((self.api.creates, len(self.api.intents)), (1, 1))

    def test_an_exact_payment_is_paid_and_gets_a_receipt(self):
        r = self.checkout()
        self.api.shopper_pays(r.order.payment_id, r.decision.charge, quote=r.decision.fx_quote_id)
        order = self.shop.confirm(r.order.payment_id)
        self.assertEqual(order.status, "paid")
        signed = release(order, self.signer, self.store)
        self.assertTrue(receipts.verify(signed, self.key.public_key(), self.approval).ok)
        self.assertIsNone(self.shop.handoff("k1"))
        self.assertEqual(self.api.refunds, [])

    def test_a_payment_that_does_not_match_is_refunded_with_no_receipt(self):
        r = self.checkout()
        self.api.shopper_pays(r.order.payment_id, r.decision.charge + 500, quote=r.decision.fx_quote_id)
        order = self.shop.confirm(r.order.payment_id)
        self.assertEqual(order.status, "refunded")
        self.assertEqual([x["payment_intent_id"] for x in self.api.refunds], [r.order.payment_id])
        self.assertIsNone(release(order, self.signer, self.store))
        self.assertEqual(self.shop.catalog[self.sku].stock, self.stock)
        self.assertTrue(any("allowed" in p for p in self.shop.problems["k1"]))

    def test_a_webhook_twice_settles_once(self):
        r = self.checkout()
        self.api.shopper_pays(r.order.payment_id, r.decision.charge + 500)
        self.shop.confirm(r.order.payment_id)
        self.shop.confirm(r.order.payment_id)
        self.assertEqual(len(self.api.refunds), 1)

    def test_the_event_body_is_never_trusted(self):
        r = self.checkout()  # the intent is still unpaid at Airwallex, whatever an event claims
        self.assertEqual(self.shop.confirm(r.order.payment_id).status, "pending")

    def test_a_cancelled_payment_releases_the_stock(self):
        r = self.checkout()
        self.api.shopper_pays(r.order.payment_id, r.decision.charge, status="CANCELLED")
        self.assertEqual(self.shop.confirm(r.order.payment_id).status, "failed")
        self.assertEqual(self.shop.catalog[self.sku].stock, self.stock)
        self.assertEqual(self.checkout().decision.outcome, Outcome.REFUSE)

    def test_someone_elses_intent_changes_nothing(self):
        self.assertIsNone(self.shop.confirm("int_not_ours"))

    def test_intent_of_reads_payment_intent_events_only(self):
        self.assertEqual(intent_of({"name": "payment_intent.succeeded", "data": {"object": {"id": "int_9"}}}), "int_9")
        self.assertIsNone(intent_of({"name": "refund.succeeded", "data": {"object": {"id": "rfd_1"}}}))
        self.assertIsNone(intent_of({"name": "payment_intent.succeeded"}))


if __name__ == "__main__":
    unittest.main()
