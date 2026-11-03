"""V5′: AirwallexMerchant over a fake Airwallex, end to end through the countersign (C9)."""

import contextlib
import io
import json
import unittest
import unittest.mock
import urllib.error
from datetime import datetime, timedelta, timezone
from decimal import Decimal

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from countersign import receipt as receipts
from countersign.airwallex import UNREACHABLE, Airwallex, AirwallexError, _Token
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
        self.posts = []  # every POST path, in order: a decline must add none

    def request_id(self):
        return "req"

    def post(self, path, body=None, extra_headers=None):
        self.posts.append(path)
        if path.startswith("/pa/payment_intents/") and path.endswith("/cancel"):
            intent = self.intents[path.split("/")[3]]
            if intent["status"] == "SUCCEEDED":  # too late: Airwallex refuses to cancel a paid intent
                raise AirwallexError(400, {"code": "invalid_status_for_operation"})
            intent["status"] = "CANCELLED"
            return dict(intent)
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
                                       conversion_quote_id=quote,
                                       latest_payment_attempt={"id": "att_ok", "payment_intent_id": intent_id,
                                                               "status": "CAPTURE_REQUESTED"})

    def card_declined(self, intent_id, code="authorization_failed"):
        """A card refused in Drop-in: the attempt FAILED and the intent waits for another payment method.

        code=None is an attempt that FAILED with no failure code at all.
        """
        attempt = {"id": "att_1", "payment_intent_id": intent_id, "status": "FAILED"}
        if code is not None:
            attempt.update(failure_code=code, failure_details={"code": code, "message": "Do not honor"})
        self.intents[intent_id].update(status="REQUIRES_PAYMENT_METHOD", latest_payment_attempt=attempt)


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

    # -- A16: a declined card

    def declined(self, code="authorization_failed"):
        r = self.checkout()
        self.api.card_declined(r.order.payment_id, code)
        return r, self.shop.confirm(r.order.payment_id)

    def test_a_declined_card_charges_nothing_signs_nothing_and_keeps_the_payment_open(self):
        r, order = self.declined()
        self.assertEqual(order.status, "pending")  # the shopper can still pay this order with another card
        self.assertEqual(self.shop.declines["k1"], "authorization_failed")
        self.assertIsNone(release(order, self.signer, self.store))
        self.assertEqual(self.api.refunds, [])
        self.assertIsNotNone(self.shop.handoff("k1"))  # Drop-in can mount again on the same intent
        self.assertEqual(self.shop.catalog[self.sku].stock, self.stock - 1)

    def test_a_decline_is_never_retried_and_a_retried_checkout_starts_no_second_intent(self):
        self.declined()
        self.shop.confirm(self.api.intents["int_1"]["id"])  # the webhook again
        again = self.checkout()  # the agent completes again after the decline
        self.assertEqual(again.order.status, "pending")
        self.assertEqual((self.api.creates, len(self.api.intents)), (1, 1))
        self.assertEqual(self.api.posts, ["/pa/payment_intents/create"])  # nothing else asked of Airwallex

    def test_another_card_after_a_decline_is_one_order_one_charge_one_receipt(self):
        r, _ = self.declined()
        self.api.shopper_pays(r.order.payment_id, r.decision.charge, quote=r.decision.fx_quote_id)
        order = self.shop.confirm(r.order.payment_id)
        self.assertEqual(order.status, "paid")
        self.assertNotIn("k1", self.shop.declines)
        signed = release(order, self.signer, self.store)
        self.assertTrue(receipts.verify(signed, self.key.public_key(), self.approval).ok)
        self.assertEqual((len(self.shop.orders), self.api.creates, self.api.refunds), (1, 1, []))

    def test_a_failed_attempt_with_no_code_is_kept_as_failed_with_no_reason(self):
        self.declined(code=None)
        self.assertIn("k1", self.shop.declines)
        self.assertIsNone(self.shop.declines["k1"])  # never a made-up code

    def test_cancelling_an_open_payment_before_any_card_is_tried(self):
        r = self.checkout()
        self.assertEqual(self.shop.cancel_payment("k1").status, "failed")
        self.assertEqual(self.api.intents[r.order.payment_id]["status"], "CANCELLED")
        self.assertEqual(self.shop.catalog[self.sku].stock, self.stock)

    def test_a_new_attempt_clears_the_decline(self):
        r, _ = self.declined()
        self.api.intents[r.order.payment_id].update(
            status="REQUIRES_CUSTOMER_ACTION", latest_payment_attempt={"status": "AUTHENTICATION_REDIRECTED"})
        self.assertEqual(self.shop.confirm(r.order.payment_id).status, "pending")
        self.assertNotIn("k1", self.shop.declines)

    def test_cancelling_after_a_decline_closes_the_intent_and_gives_the_stock_back(self):
        r, _ = self.declined()
        order = self.shop.cancel_payment("k1")
        self.assertEqual(order.status, "failed")
        self.assertEqual(self.api.intents[r.order.payment_id]["status"], "CANCELLED")  # no later card can pay it
        self.assertEqual(self.api.posts, ["/pa/payment_intents/create", f"/pa/payment_intents/{r.order.payment_id}/cancel"])
        self.assertEqual(self.shop.catalog[self.sku].stock, self.stock)
        self.assertIsNone(self.shop.handoff("k1"))
        self.assertNotIn("k1", self.shop.declines)

    def test_a_cancel_that_loses_the_race_to_another_card_keeps_the_payment(self):
        r, _ = self.declined()
        self.api.shopper_pays(r.order.payment_id, r.decision.charge, quote=r.decision.fx_quote_id)
        self.assertEqual(self.shop.cancel_payment("k1").status, "paid")  # Airwallex refused the cancel
        self.assertEqual((self.api.refunds, self.shop.catalog[self.sku].stock), ([], self.stock - 1))

    def test_a_retried_start_keeps_its_order_number_when_another_order_came_between(self):
        # The first create reaches Airwallex but its answer is lost; another shopper's order lands; the retry
        # gets Airwallex's first intent back by request_id. It must carry the same order number, or
        # check_paid refunds a good payment.
        create = self.api.post

        def lost_answer(path, body=None, extra_headers=None):
            create(path, body, extra_headers)
            raise TimeoutError("the answer never came")

        self.api.post = lost_answer
        with self.assertRaises(TimeoutError):
            self.checkout("k1")
        self.api.post = create
        other = self.checkout("k2")
        first = self.checkout("k1")
        self.assertNotEqual(first.order.order_number, other.order.order_number)
        intent = self.api.intents[first.order.payment_id]
        self.assertEqual(intent["merchant_order_id"], first.order.order_number)
        self.api.shopper_pays(first.order.payment_id, first.decision.charge, quote=first.decision.fx_quote_id)
        self.assertEqual(self.shop.confirm(first.order.payment_id).status, "paid")
        self.assertEqual(self.api.refunds, [])

    def test_a_cancel_airwallex_never_answers_leaves_the_order_open(self):
        r = self.checkout()

        def timed_out(*args, **kwargs):
            raise TimeoutError("timed out")

        self.api.post = self.api.get = timed_out
        self.assertEqual(self.shop.cancel_payment("k1").status, "pending")  # the caller can try again
        self.assertEqual(self.shop.catalog[self.sku].stock, self.stock - 1)

    def test_payment_attempt_events_name_their_intent(self):
        for name in ("payment_attempt.authorization_failed", "payment_attempt.authentication_failed"):
            event = {"name": name, "data": {"object": {"id": "att_1", "payment_intent_id": "int_9"}}}
            self.assertEqual(intent_of(event), "int_9")
        self.assertIsNone(intent_of({"name": "payment_attempt.authorization_failed", "data": {"object": {"id": "att_1"}}}))


class ClientRetryTest(unittest.TestCase):
    """The HTTP client: a GET that gets no answer is tried once more; a POST never is."""

    def setUp(self):
        self.client = Airwallex("id", "key")
        self.client._token = _Token("tok", float("inf"))
        self.calls = []

    def urlopen(self, *answers):
        answers = list(answers)

        def fake(req, timeout=None):
            self.calls.append(req.get_method())
            answer = answers.pop(0)
            if isinstance(answer, BaseException):
                raise answer
            return contextlib.nullcontext(io.BytesIO(json.dumps(answer).encode()))

        return unittest.mock.patch("urllib.request.urlopen", fake), unittest.mock.patch("time.sleep")

    def test_a_get_without_an_answer_is_tried_once_more(self):
        opener, sleep = self.urlopen(TimeoutError("timed out"), {"id": "int_1"})
        with opener, sleep:
            self.assertEqual(self.client.get("/pa/payment_intents/int_1"), {"id": "int_1"})
        self.assertEqual(self.calls, ["GET", "GET"])

    def test_a_get_that_fails_twice_raises_what_happened(self):
        opener, sleep = self.urlopen(urllib.error.URLError("refused"), TimeoutError("timed out"))
        with opener, sleep, self.assertRaises(UNREACHABLE):
            self.client.get("/pa/payment_intents/int_1")

    def test_a_post_without_an_answer_is_not_sent_again(self):
        opener, sleep = self.urlopen(TimeoutError("timed out"), {"id": "int_1"})
        with opener, sleep, self.assertRaises(TimeoutError):
            self.client.post("/pa/payment_intents/create", {"request_id": "r1"})
        self.assertEqual(self.calls, ["POST"])

    def test_an_http_error_is_an_answer_not_an_outage(self):
        err = urllib.error.HTTPError("u", 400, "Bad", {}, io.BytesIO(b'{"code": "invalid_argument"}'))
        opener, sleep = self.urlopen(err)
        with opener, sleep, self.assertRaises(AirwallexError) as caught:
            self.client.get("/pa/payment_intents/int_1")
        self.assertEqual((caught.exception.status, self.calls), (400, ["GET"]))


if __name__ == "__main__":
    unittest.main()
