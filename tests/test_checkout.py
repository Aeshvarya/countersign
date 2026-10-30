import unittest
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from decimal import Decimal

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from countersign import receipt as receipts
from countersign.checkout import MemoryReceiptStore, countersign, release
from countersign.decide import Outcome
from countersign.fx import FakeFx, _parse_time
from countersign.merchant import FakeMerchant, synthetic_catalog
from countersign.terms import Approval

NOW = datetime(2026, 11, 2, 10, 0, tzinfo=timezone.utc)


class CatalogTest(unittest.TestCase):
    def test_at_least_100_unique_products(self):
        catalog = synthetic_catalog()
        self.assertGreaterEqual(len(catalog), 100)
        self.assertEqual(len({p.sku for p in catalog}), len(catalog))

    def test_catalog_is_deterministic(self):
        self.assertEqual(synthetic_catalog(), synthetic_catalog())


class CountersignTest(unittest.TestCase):
    def setUp(self):
        self.fx = FakeFx("84.10")
        self.shop = FakeMerchant(self.fx)
        self.sku = self.shop.search("keyboard")[0].sku
        terms = self.shop.offer({self.sku: 1}, "INR", NOW)
        self.approval = Approval.grant(terms, terms.shopper_total + 200, "s", NOW, NOW + timedelta(hours=2))

    def test_proceed_returns_a_merchant_order_number(self):
        r = countersign(self.shop, self.approval, "k1", NOW)
        self.assertEqual(r.decision.outcome, Outcome.PROCEED)
        self.assertTrue(r.order.order_number.startswith("ORD-"))
        self.assertEqual(r.order.currency, "INR")

    def test_retry_with_the_same_key_never_pays_twice(self):
        first = countersign(self.shop, self.approval, "k1", NOW)
        stock_after_first = self.shop.catalog[self.sku].stock
        second = countersign(self.shop, self.approval, "k1", NOW)
        self.assertEqual(first.order, second.order)
        self.assertEqual(len(self.shop.orders), 1)
        self.assertEqual(self.shop.catalog[self.sku].stock, stock_after_first)

    def test_the_same_key_under_a_different_approval_is_refused_not_reported_as_paid(self):
        countersign(self.shop, self.approval, "k1", NOW)
        other = Approval.grant(self.approval.terms, self.approval.ceiling + 50, "s", NOW, NOW + timedelta(hours=2))
        r = countersign(self.shop, other, "k1", NOW)
        self.assertEqual(r.decision.outcome, Outcome.REFUSE)
        self.assertIsNone(r.order)
        self.assertEqual(len(self.shop.orders), 1)

    def test_the_order_records_the_approval_it_was_charged_under(self):
        r = countersign(self.shop, self.approval, "k1", NOW)
        self.assertEqual(r.order.approval_hash, self.approval.hash)

    def test_out_of_stock_asks_again_without_charging(self):
        self.shop.set_stock(self.sku, 0)
        r = countersign(self.shop, self.approval, "k1", NOW)
        self.assertEqual(r.decision.outcome, Outcome.REASK)
        self.assertIsNone(r.order)
        self.assertEqual(self.shop.orders, {})

    def test_price_rise_asks_again_without_charging(self):
        self.shop.set_price(self.sku, Decimal("199.99"))
        r = countersign(self.shop, self.approval, "k1", NOW)
        self.assertEqual(r.decision.outcome, Outcome.REASK)
        self.assertEqual(self.shop.orders, {})

    def test_pay_refuses_without_a_proceed_decision(self):
        from countersign.decide import Decision

        terms = self.shop.offer({self.sku: 1}, "INR", NOW)
        with self.assertRaises(PermissionError):
            self.shop.pay(terms, Decision(Outcome.REASK, "no"), "k1")


class PendingMerchant(FakeMerchant):
    """Pays like a card in the browser: the order is pending until confirmed."""

    def pay(self, terms, decision, idempotency_key, approval_hash=None):
        order = super().pay(terms, decision, idempotency_key, approval_hash)
        self.orders[idempotency_key] = replace(order, status="pending")
        return self.orders[idempotency_key]

    def settle(self, idempotency_key, status):
        self.orders[idempotency_key] = replace(self.orders[idempotency_key], status=status)
        return self.orders[idempotency_key]


class HeldReceiptTest(unittest.TestCase):
    """C9: the receipt is signed only after the payment is checked."""

    def setUp(self):
        self.shop = PendingMerchant(FakeFx("84.10"))
        self.sku = self.shop.search("keyboard")[0].sku
        terms = self.shop.offer({self.sku: 1}, "INR", NOW)
        self.approval = Approval.grant(terms, terms.shopper_total, "s", NOW, NOW + timedelta(hours=2))
        self.key = Ed25519PrivateKey.generate()
        self.signer = receipts.MerchantSigner(self.key, "m-1")
        self.store = MemoryReceiptStore()

    def run_checkout(self):
        return countersign(self.shop, self.approval, "k1", NOW, signer=self.signer, store=self.store)

    def test_a_pending_payment_gets_no_receipt_yet(self):
        r = self.run_checkout()
        self.assertEqual((r.decision.outcome, r.order.status, r.receipt), (Outcome.PROCEED, "pending", None))
        self.assertIsNone(self.store.get("k1"))
        self.assertIsNotNone(self.store.held("k1"))

    def test_the_receipt_is_signed_once_the_payment_is_checked(self):
        self.run_checkout()
        signed = release(self.shop.settle("k1", "paid"), self.signer, self.store)
        self.assertTrue(receipts.verify(signed, self.key.public_key(), self.approval).ok)
        self.assertIs(release(self.shop.orders["k1"], self.signer, self.store), signed)  # a second webhook
        self.assertIsNone(self.store.held("k1"))

    def test_a_refunded_payment_never_gets_a_receipt(self):
        self.run_checkout()
        self.assertIsNone(release(self.shop.settle("k1", "refunded"), self.signer, self.store))
        self.assertIsNone(self.store.get("k1"))
        self.assertIsNone(self.store.held("k1"))

    def test_a_retry_while_pending_does_not_charge_again(self):
        first = self.run_checkout()
        again = self.run_checkout()
        self.assertEqual(again.order, first.order)
        self.assertIn("not finished", again.decision.reason)
        self.assertEqual(len(self.shop.orders), 1)

    def test_a_retry_after_a_refund_is_refused_not_reported_as_paid(self):
        self.run_checkout()
        self.shop.settle("k1", "refunded")
        r = self.run_checkout()
        self.assertEqual(r.decision.outcome, Outcome.REFUSE)
        self.assertEqual(len(self.shop.orders), 1)


class FxParseTest(unittest.TestCase):
    def test_both_airwallex_time_formats(self):
        self.assertEqual(_parse_time("2025-09-18T07:13:34Z"), datetime(2025, 9, 18, 7, 13, 34, tzinfo=timezone.utc))
        self.assertEqual(_parse_time("2019-05-04T02:49:05+0000"), datetime(2019, 5, 4, 2, 49, 5, tzinfo=timezone.utc))
        self.assertEqual(_parse_time("2024-03-21T10:08:12+1100"), datetime(2024, 3, 20, 23, 8, 12, tzinfo=timezone.utc))


if __name__ == "__main__":
    unittest.main()
