import unittest
from decimal import Decimal

from countersign.decide import Decision, Outcome
from countersign.payments import AirwallexPayments, PaymentIntent, check_paid

PROCEED = Decision(Outcome.PROCEED, "ok", charge=Decimal("10763.96"), currency="INR", fx_quote_id="cvq_1")


class FakeAirwallex:
    """Records calls; answers like the sandbox (shapes from the Airwallex docs)."""

    def __init__(self):
        self.calls = []
        self.intents = {}

    def request_id(self):
        return "req-1"

    def post(self, path, body=None, extra_headers=None):
        self.calls.append(("POST", path, body))
        if path == "/pa/payment_intents/create":
            existing = next((i for i in self.intents.values() if i["request_id"] == body["request_id"]), None)
            if existing:
                return existing
            intent = {"id": f"int_{len(self.intents) + 1}", "request_id": body["request_id"], "amount": body["amount"],
                      "currency": body["currency"], "status": "REQUIRES_PAYMENT_METHOD",
                      "merchant_order_id": body["merchant_order_id"], "metadata": body["metadata"],
                      "client_secret": "secret-1"}
            self.intents[intent["id"]] = intent
            return intent
        raise AssertionError(path)

    def get(self, path, params=None):
        self.calls.append(("GET", path, None))
        return self.intents[path.rsplit("/", 1)[1]]


def paid(**overrides):
    base = dict(id="int_1", status="SUCCEEDED", amount=Decimal("10763.96"), currency="INR",
                merchant_order_id="ORD-1001", conversion_quote_id="cvq_1", metadata={"approval_hash": "h1"})
    base.update(overrides)
    return PaymentIntent(**base)


class StartTest(unittest.TestCase):
    def setUp(self):
        self.fake = FakeAirwallex()
        self.payments = AirwallexPayments(self.fake)

    def start(self, decision=PROCEED):
        return self.payments.start(decision, merchant_total=Decimal("127.99"), merchant_currency="USD",
                                   order_number="ORD-1001", approval_hash="h1", idempotency_key="key-1")

    def test_the_intent_is_in_the_merchant_currency_and_drop_in_gets_the_quote(self):
        handoff = self.start()
        _, path, body = self.fake.calls[0]
        self.assertEqual(path, "/pa/payment_intents/create")
        self.assertEqual((body["amount"], body["currency"], body["merchant_order_id"]), (127.99, "USD", "ORD-1001"))
        self.assertEqual(body["metadata"]["approval_hash"], "h1")
        self.assertEqual(handoff.as_json(), {"intent_id": "int_1", "client_secret": "secret-1",
                                             "currency": "USD", "quote_id": "cvq_1"})

    def test_a_retried_start_reuses_the_same_intent(self):
        self.assertEqual(self.start().intent_id, self.start().intent_id)
        self.assertEqual(len(self.fake.intents), 1)

    def test_no_intent_without_a_proceed(self):
        with self.assertRaises(PermissionError):
            self.start(Decision(Outcome.REASK, "changed"))
        self.assertEqual(self.fake.calls, [])


class CheckPaidTest(unittest.TestCase):
    def check(self, intent):
        return check_paid(intent, PROCEED, order_number="ORD-1001", approval_hash="h1")

    def test_exactly_the_allowed_payment_passes(self):
        self.assertTrue(self.check(paid()).ok)

    def test_one_minor_unit_of_rounding_is_tolerated(self):
        self.assertTrue(self.check(paid(amount=Decimal("10763.97"))).ok)

    def test_every_mismatch_is_named(self):
        cases = {
            "not succeeded": (paid(status="REQUIRES_PAYMENT_METHOD"), "not SUCCEEDED"),
            "other order": (paid(merchant_order_id="ORD-9"), "for order ORD-9"),
            "other approval": (paid(metadata={"approval_hash": "h2"}), "approval's hash"),
            "other currency": (paid(currency="USD", amount=Decimal("127.99")), "charged in USD"),
            "more money": (paid(amount=Decimal("11000.00")), "allowed 10763.96"),
            "other rate lock": (paid(conversion_quote_id="cvq_9"), "different rate lock"),
        }
        for name, (intent, words) in cases.items():
            with self.subTest(name):
                check = self.check(intent)
                self.assertFalse(check.ok)
                self.assertTrue(any(words in r for r in check.reasons), check.reasons)


if __name__ == "__main__":
    unittest.main()
