import copy
import unittest
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from decimal import Decimal

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from countersign import receipt
from countersign.checkout import MemoryReceiptStore, countersign
from countersign.fx import FakeFx
from countersign.merchant import FakeMerchant
from countersign.tap import AgentIdentity
from countersign.terms import Approval

NOW = datetime(2026, 11, 2, 10, 0, tzinfo=timezone.utc)
AGENT = AgentIdentity(key_id="agent-key-1", signature_agent="https://agents.example", tag="agent-payer-auth", nonce="n-1")


class ReceiptTest(unittest.TestCase):
    def setUp(self):
        self.merchant_key = Ed25519PrivateKey.generate()
        self.signer = receipt.MerchantSigner(self.merchant_key, "merchant-key-1")
        self.store = MemoryReceiptStore()
        self.shop = FakeMerchant(FakeFx("84.10"))
        sku = self.shop.search("headphones")[0].sku
        terms = self.shop.offer({sku: 1}, "INR", NOW)
        self.approval = Approval.grant(terms, terms.shopper_total + 150, "shopper@example.com", NOW, NOW + timedelta(hours=1))

    def pay(self, key="k1"):
        return countersign(self.shop, self.approval, key, NOW, agent=AGENT, signer=self.signer, store=self.store)

    def test_a_paid_order_comes_with_a_receipt_that_verifies(self):
        r = self.pay()
        check = receipt.verify(r.receipt, self.merchant_key.public_key(), self.approval)
        self.assertTrue(check.ok, check.findings)
        body = r.receipt["body"]
        self.assertEqual(body["order_number"], r.order.order_number)
        self.assertEqual(body["approval"]["hash"], self.approval.hash)
        self.assertEqual(body["agent"]["key_id"], "agent-key-1")
        self.assertEqual(body["charged"]["fx_quote_id"], r.decision.fx_quote_id)

    def test_a_retry_returns_the_same_receipt(self):
        first, second = self.pay(), self.pay()
        self.assertEqual(first.receipt, second.receipt)

    def test_an_edited_receipt_fails(self):
        forged = copy.deepcopy(self.pay().receipt)
        forged["body"]["charged"]["amount"] = "1.00"
        check = receipt.verify(forged, self.merchant_key.public_key())
        self.assertFalse(check.ok)
        self.assertIn("signature does not match", check.findings[0])

    def test_another_merchants_key_fails(self):
        self.assertFalse(receipt.verify(self.pay().receipt, Ed25519PrivateKey.generate().public_key()).ok)

    def test_a_receipt_for_a_different_approval_fails(self):
        r = self.pay()
        other = Approval.grant(self.approval.terms, self.approval.ceiling + 1, "s", NOW, NOW + timedelta(hours=1))
        check = receipt.verify(r.receipt, self.merchant_key.public_key(), other)
        self.assertFalse(check.ok)
        self.assertIn("different approval", check.findings[-1])

    def test_a_charge_above_the_ceiling_fails_even_if_signed(self):
        body = copy.deepcopy(self.pay().receipt["body"])
        body["charged"]["amount"] = format(Decimal(body["approval"]["ceiling"]) + 1, "f")
        check = receipt.verify(receipt.sign(body, self.signer), self.merchant_key.public_key())
        self.assertFalse(check.ok)
        self.assertIn("above the approved ceiling", check.findings[-1])

    def test_no_receipt_without_a_signing_key(self):
        self.assertIsNone(countersign(self.shop, self.approval, "k9", NOW).receipt)

    def test_no_receipt_when_the_answer_is_ask_again(self):
        self.shop.set_delivery(self.approval.terms.items[0].sku, "standard, 12-15 business days")
        r = self.pay()
        self.assertIsNone(r.order)
        self.assertIsNone(r.receipt)
        self.assertIsNone(replace(r.decision).charge)


if __name__ == "__main__":
    unittest.main()
