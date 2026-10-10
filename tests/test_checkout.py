import unittest
from datetime import datetime, timedelta, timezone
from decimal import Decimal

from countersign.checkout import countersign
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


class FxParseTest(unittest.TestCase):
    def test_both_airwallex_time_formats(self):
        self.assertEqual(_parse_time("2025-09-18T07:13:34Z"), datetime(2025, 9, 18, 7, 13, 34, tzinfo=timezone.utc))
        self.assertEqual(_parse_time("2019-05-04T02:49:05+0000"), datetime(2019, 5, 4, 2, 49, 5, tzinfo=timezone.utc))
        self.assertEqual(_parse_time("2024-03-21T10:08:12+1100"), datetime(2024, 3, 20, 23, 8, 12, tzinfo=timezone.utc))


if __name__ == "__main__":
    unittest.main()
