import unittest
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from decimal import Decimal

from countersign.decide import Outcome, decide
from countersign.terms import Approval, FxLock, LineItem, Terms, money

NOW = datetime(2026, 11, 2, 10, 0, tzinfo=timezone.utc)


def offer(rate="84.10", price="127.99", qty=1, delivery="standard, 5-7 business days", lock_minutes=60, quote="cvq_1"):
    items = (LineItem("DES-1", "Keyboard", qty, Decimal(price)),)
    merchant_total = money(Decimal(price) * qty, "USD")
    fx = FxLock(quote, "USD", "INR", Decimal(rate), NOW + timedelta(minutes=lock_minutes))
    return Terms("m1", items, delivery, "USD", merchant_total, "INR", money(merchant_total * Decimal(rate), "INR"), fx)


def approve(terms, ceiling=None, hours=2):
    return Approval.grant(terms, ceiling or terms.shopper_total, "s@example.com", NOW, NOW + timedelta(hours=hours))


class DecideTest(unittest.TestCase):
    def test_unchanged_terms_proceed_at_the_locked_rate(self):
        terms = offer()
        d = decide(approve(terms), terms, NOW)
        self.assertEqual(d.outcome, Outcome.PROCEED)
        self.assertEqual((d.charge, d.currency, d.fx_quote_id), (Decimal("10763.96"), "INR", "cvq_1"))

    def test_rate_drift_under_the_ceiling_proceeds_silently(self):
        approval = approve(offer(), ceiling=Decimal("11000"))
        d = decide(approval, offer(rate="84.90", quote="cvq_2"), NOW)
        self.assertEqual(d.outcome, Outcome.PROCEED)
        self.assertEqual(d.fx_quote_id, "cvq_2")

    def test_rate_drift_over_the_ceiling_asks_again(self):
        approval = approve(offer(), ceiling=Decimal("11000"))
        d = decide(approval, offer(rate="88.40"), NOW)
        self.assertEqual(d.outcome, Outcome.REASK)
        self.assertEqual(d.changes[0].field, "shopper_total")

    def test_ceiling_is_inclusive(self):
        terms = offer()
        self.assertEqual(decide(approve(terms, ceiling=terms.shopper_total), terms, NOW).outcome, Outcome.PROCEED)

    def test_price_change_asks_again_even_if_cheaper(self):
        d = decide(approve(offer()), offer(price="119.99"), NOW)
        self.assertEqual(d.outcome, Outcome.REASK)
        self.assertEqual({c.field for c in d.changes}, {"items", "merchant_total"})

    def test_delivery_or_quantity_change_asks_again(self):
        approval = approve(offer())
        self.assertEqual(decide(approval, offer(delivery="12-15 days"), NOW).changes[0].field, "delivery")
        self.assertEqual(decide(approval, offer(qty=2), NOW).outcome, Outcome.REASK)

    def test_expired_approval_asks_again(self):
        terms = offer(lock_minutes=600)
        self.assertEqual(decide(approve(terms, hours=1), terms, NOW + timedelta(hours=2)).outcome, Outcome.REASK)

    def test_tampered_approval_is_refused(self):
        terms = offer()
        approval = replace(approve(terms), ceiling=Decimal("999999"))
        self.assertEqual(decide(approval, terms, NOW).outcome, Outcome.REFUSE)

    def test_expired_rate_lock_is_refused_not_shown_to_the_shopper(self):
        terms = offer(lock_minutes=1)
        self.assertEqual(decide(approve(terms), terms, NOW + timedelta(minutes=2)).outcome, Outcome.REFUSE)

    def test_a_shopper_total_that_does_not_follow_from_the_rate_is_refused(self):
        terms = offer()
        forged = replace(terms, shopper_total=Decimal("100.00"))
        self.assertEqual(decide(approve(terms), forged, NOW).outcome, Outcome.REFUSE)

    def test_lines_that_do_not_add_up_are_refused(self):
        terms = offer()
        self.assertEqual(decide(approve(terms), replace(terms, merchant_total=Decimal("1.00")), NOW).outcome, Outcome.REFUSE)

    def test_cross_currency_without_a_rate_lock_is_refused(self):
        terms = offer()
        self.assertEqual(decide(approve(terms), replace(terms, fx=None), NOW).outcome, Outcome.REFUSE)

    def test_same_currency_needs_no_rate_lock(self):
        items = (LineItem("DES-1", "Keyboard", 1, Decimal("127.99")),)
        terms = Terms("m1", items, "standard", "USD", Decimal("127.99"), "USD", Decimal("127.99"))
        self.assertEqual(decide(approve(terms), terms, NOW).outcome, Outcome.PROCEED)

    def test_ceiling_below_the_shown_price_cannot_be_granted(self):
        terms = offer()
        with self.assertRaises(ValueError):
            Approval.grant(terms, Decimal("1"), "s", NOW, NOW + timedelta(hours=1))


if __name__ == "__main__":
    unittest.main()
