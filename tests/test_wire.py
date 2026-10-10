import json
import unittest
from datetime import datetime, timedelta, timezone

from countersign import wire
from countersign.checkout import countersign
from countersign.decide import Outcome, decide
from countersign.fx import FakeFx
from countersign.merchant import FakeMerchant

NOW = datetime(2026, 11, 2, 10, 0, tzinfo=timezone.utc)


class WireTest(unittest.TestCase):
    def setUp(self):
        from countersign.terms import Approval

        self.shop = FakeMerchant(FakeFx("84.10"))
        sku = self.shop.search("kettle")[0].sku
        self.terms = self.shop.offer({sku: 2}, "INR", NOW)
        self.approval = Approval.grant(self.terms, self.terms.shopper_total, "s", NOW, NOW + timedelta(hours=1))

    def roundtrip(self, to, frm, value):
        return frm(json.loads(json.dumps(to(value))))

    def test_terms_and_approval_survive_json(self):
        self.assertEqual(self.roundtrip(wire.terms_to_json, wire.terms_from_json, self.terms), self.terms)
        back = self.roundtrip(wire.approval_to_json, wire.approval_from_json, self.approval)
        self.assertEqual(back, self.approval)
        self.assertTrue(back.is_intact())

    def test_an_approval_edited_in_transit_is_refused(self):
        data = wire.approval_to_json(self.approval)
        data["ceiling"] = "999999.00"
        self.assertEqual(decide(wire.approval_from_json(data), self.terms, NOW).outcome, Outcome.REFUSE)

    def test_amounts_are_strings_not_floats(self):
        data = wire.terms_to_json(self.terms)
        self.assertIsInstance(data["shopper_total"], str)
        self.assertIsInstance(data["fx"]["rate"], str)

    def test_decision_and_order_survive_json(self):
        r = countersign(self.shop, self.approval, "k1", NOW)
        self.assertEqual(self.roundtrip(wire.decision_to_json, wire.decision_from_json, r.decision), r.decision)
        self.assertEqual(self.roundtrip(wire.order_to_json, wire.order_from_json, r.order), r.order)


if __name__ == "__main__":
    unittest.main()
