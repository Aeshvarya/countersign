"""W3: a payment Airwallex's MCP made, settled against the quote our create_checkout gave.

The intents are shaped like the first live one (2026-11-05, int_sgpvhhzcphnvd9yy4qd): merchant_order_id is
our checkout id, and the latest attempt names the chosen option under agentic_payment_data.fulfillment.
"""

import base64
import copy
import json
import unittest
from datetime import datetime, timedelta, timezone
from decimal import Decimal

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from countersign import receipt, tap
from countersign.checkout import MemoryReceiptStore
from countersign.fx import FakeFx
from countersign.merchant import FakeMerchant

try:
    from fastapi.testclient import TestClient

    from countersign.edge import create_app, hmac_auth
    from countersign.edge.acp import AcpConfig, from_minor
    from countersign.settle import Settler, problems, selected_option
    from countersign.web.host import WEBHOOK_PATH, Host, house_from_env
except ImportError:  # the server extra is not installed
    TestClient = None

try:
    from tests.test_host import demo_app
except ImportError:  # run by discover with -s tests
    from test_host import demo_app

SECRET = b"k" * 32
NOW = datetime(2026, 11, 5, 7, 16, tzinfo=timezone.utc)


class Clock:
    def __init__(self):
        self.now = NOW

    def __call__(self):
        return self.now


class FakeAirwallex:
    """Airwallex's record of intents, and the refunds asked of it."""

    def __init__(self):
        self.intents: dict[str, dict] = {}
        self.refunds: list[dict] = []
        self.gets = 0

    def get(self, path, params=None):
        self.gets += 1
        if path == "/pa/payment_intents":  # the list, filtered by merchant_order_id
            items = [i for i in self.intents.values() if i["merchant_order_id"] == params["merchant_order_id"]]
            return {"has_more": False, "items": copy.deepcopy(items)}
        return copy.deepcopy(self.intents[path.rsplit("/", 1)[-1]])

    def post(self, path, body=None, extra_headers=None):
        assert path == "/pa/refunds/create", path
        self.refunds.append(body)
        return {"id": f"rfd_{len(self.refunds)}", "status": "RECEIVED"}


def intent(intent_id, checkout_id, amount, currency="INR", option="standard", status="SUCCEEDED",
           created=NOW + timedelta(minutes=1)):
    fulfillment = [] if option is None else [{
        "id": "shipping", "type": "shipping", "line_item_ids": ["line-1"],
        "selected_destination_id": "shipping_address", "selected_option_id": option}]
    return {
        "id": intent_id, "status": status, "amount": float(amount) if isinstance(amount, str) else amount,
        "currency": currency, "merchant_order_id": checkout_id, "request_id": "fe6ca395",
        "created_at": created.isoformat(), "captured_amount": amount,
        "latest_payment_attempt": {
            "status": "CAPTURE_REQUESTED", "payment_intent_id": intent_id, "merchant_order_id": checkout_id,
            "agentic_payment_data": {"checkout_session_id": "1eeba3cd", "metadata": {}, "fulfillment": fulfillment},
        },
    }


@unittest.skipIf(TestClient is None, "needs the server extra: pip install -e '.[server]' httpx")
class SettleTest(unittest.TestCase):
    def setUp(self):
        self.clock = Clock()
        self.merchant = FakeMerchant(FakeFx("84.10"))
        self.signer = receipt.MerchantSigner(Ed25519PrivateKey.generate(), "m-1")
        app = create_app(self.merchant, tap.StaticDirectory({}), authority="testserver", clock=self.clock,
                         signer=self.signer, acp=AcpConfig(secret=SECRET, public_url="https://shop.example"))
        self.api = app.state.acp
        self.store = MemoryReceiptStore()
        self.airwallex = FakeAirwallex()
        self.settler = Settler(self.airwallex, self.api.quote_for, self.signer, self.store, clock=lambda: NOW)
        self.sku = list(self.merchant.catalog)[0]

    def quote(self, quantity=1, currency="INR"):
        status, out = self.api.create_checkout({
            "line_items": [{"id": "line-1", "item_id": self.sku, "requested_quantity": quantity}],
            "currency": currency, "customer": {"email": "shopper@example.com"}})
        self.assertEqual(status, 200)
        return self.api.quote_for(out["id"])

    def pays(self, quote, *, option="standard", extra_minor=0, intent_id="int_1", **kw):
        amount = from_minor(quote.total_minor + quote.options.get(option, 0) + extra_minor, quote.currency)
        self.airwallex.intents[intent_id] = intent(intent_id, quote.checkout_id, format(amount, "f"), option=option, **kw)
        return intent_id

    # -- paid as quoted

    def test_a_payment_that_matches_its_quote_gets_a_receipt_anyone_can_verify(self):
        q = self.quote(2)
        done = self.settler.settle(self.pays(q))
        self.assertEqual((done.status, done.checkout_id, done.reasons), ("paid", q.checkout_id, ()))
        self.assertEqual(self.airwallex.refunds, [])
        signed = self.store.get(q.checkout_id)
        self.assertIs(signed, done.receipt)
        check = receipt.verify(signed, self.signer.key.public_key(), q.approval)
        self.assertTrue(check.ok, check.findings)
        body = signed["body"]
        self.assertEqual((body["order_number"], body["payment_id"]), (q.checkout_id, "int_1"))
        self.assertEqual(body["approval"]["hash"], q.terms_hash)
        self.assertEqual(body["charged"]["fx_quote_id"], q.terms.fx.quote_id)
        self.assertEqual(body["fulfillment"], {"option_id": "standard", "amount": "0.00", "currency": "INR"})

    def test_express_shipping_is_on_the_receipt_beside_the_approved_items(self):
        q = self.quote()
        self.assertGreater(q.options["express"], 0)
        done = self.settler.settle(self.pays(q, option="express"))
        self.assertEqual(done.status, "paid", done.reasons)
        body = done.receipt["body"]
        self.assertEqual(body["fulfillment"]["option_id"], "express")
        paid = from_minor(q.total_minor + q.options["express"], "INR")
        self.assertEqual(done.amount, paid)
        self.assertEqual(Decimal(body["charged"]["amount"]) + Decimal(body["fulfillment"]["amount"]), paid)
        self.assertTrue(receipt.verify(done.receipt, self.signer.key.public_key(), q.approval).ok)

    def test_the_live_intents_option_is_read_from_its_latest_attempt(self):
        self.assertEqual(selected_option(intent("i", "chk_x", "1.00", option="express")), "express")
        self.assertIsNone(selected_option(intent("i", "chk_x", "1.00", option=None)))
        self.assertIsNone(selected_option({"id": "i"}))

    # -- anything else after money moved is refunded in full

    def assert_refunded(self, intent_id, why):
        done = self.settler.settle(intent_id)
        self.assertEqual(done.status, "refunded")
        self.assertIn(why, "; ".join(done.reasons))
        self.assertEqual(self.airwallex.refunds, [{"request_id": f"refund-{intent_id}", "payment_intent_id": intent_id,
                                                   "reason": "payment did not match the merchant's quote"}])
        self.assertIsNone(done.receipt)
        self.assertIsNone(self.store.get(done.checkout_id))
        return done

    def test_a_different_amount_is_refunded(self):
        q = self.quote()
        self.assert_refunded(self.pays(q, extra_minor=1), "for a quote of")

    def test_a_different_currency_is_refunded(self):
        q = self.quote()
        iid = self.pays(q)
        self.airwallex.intents[iid]["currency"] = "USD"
        self.assert_refunded(iid, "paid in USD, quoted in INR")

    def test_an_option_we_never_quoted_is_refunded(self):
        q = self.quote()
        self.assert_refunded(self.pays(q, option="overnight"), "'overnight' was never quoted")

    def test_no_option_chosen_is_refunded(self):
        q = self.quote()
        self.assert_refunded(self.pays(q, option=None), "no fulfillment option was chosen")

    def test_a_payment_after_the_quote_expired_is_refunded(self):
        q = self.quote()
        self.assert_refunded(self.pays(q, created=q.expires_at + timedelta(seconds=1)), "after the quote expired")

    def test_a_checkout_we_no_longer_hold_is_refunded(self):
        self.airwallex.intents["int_9"] = intent("int_9", "chk_" + "0" * 24, "100.00")
        self.assert_refunded("int_9", "no quote on record")

    def test_after_a_restart_an_older_payment_is_left_alone_not_refunded(self):
        # Varunika's 11-05 finding: the process before the restart may already have receipted it.
        self.airwallex.intents["int_old"] = intent("int_old", "chk_" + "1" * 24, "100.00", created=NOW - timedelta(minutes=5))
        done = self.settler.settle("int_old")
        self.assertEqual(done.status, "unverified")
        self.assertIn("before the service restarted", done.reasons[0])
        self.assertEqual(self.airwallex.refunds, [])
        self.assertIs(self.settler.settle("int_old"), done)  # Airwallex resending it changes nothing
        self.assertEqual(self.airwallex.gets, 1)

    def test_a_second_payment_for_a_paid_checkout_is_refunded_and_the_first_receipt_kept(self):
        q = self.quote()
        first = self.settler.settle(self.pays(q, intent_id="int_1"))
        self.assertEqual(first.status, "paid")
        second = self.settler.settle(self.pays(q, intent_id="int_2"))
        self.assertEqual(second.status, "refunded")
        self.assertEqual(second.reasons, ("this checkout was already paid by int_1",))
        self.assertEqual([r["payment_intent_id"] for r in self.airwallex.refunds], ["int_2"])
        self.assertIs(self.store.get(q.checkout_id), first.receipt)

    def test_a_quote_with_nothing_available_is_refunded(self):
        self.merchant.set_stock(self.sku, 0)
        q = self.quote()
        self.assertIsNone(q.terms)
        self.airwallex.intents["int_2"] = intent("int_2", q.checkout_id, "0.00")
        self.assert_refunded("int_2", "nothing in the cart was available")

    def test_problems_is_empty_only_for_the_exact_payment(self):
        q = self.quote()
        self.pays(q)
        self.assertEqual(problems(self.airwallex.intents["int_1"], q), ())

    # -- what is not ours, not finished, or seen twice

    def test_an_intent_that_is_not_an_acp_checkout_of_ours_changes_nothing(self):
        self.airwallex.intents["int_3"] = intent("int_3", "ORD-SPIKE-28F78E", "12537.35")
        self.assertIsNone(self.settler.settle("int_3"))
        self.assertEqual(self.airwallex.refunds, [])

    def test_a_payment_still_under_way_waits_then_settles(self):
        q = self.quote()
        iid = self.pays(q, status="REQUIRES_PAYMENT_METHOD")
        self.assertEqual(self.settler.settle(iid).status, "pending")
        self.assertIsNone(self.store.get(q.checkout_id))
        self.airwallex.intents[iid]["status"] = "SUCCEEDED"
        self.assertEqual(self.settler.settle(iid).status, "paid")

    def test_a_repeated_event_settles_once(self):
        q = self.quote()
        iid = self.pays(q, extra_minor=5)
        first = self.settler.settle(iid)
        self.assertIs(self.settler.settle(iid), first)
        self.assertEqual((len(self.airwallex.refunds), self.airwallex.gets), (1, 1))


WEBHOOK_SECRET = "w" * 32


@unittest.skipIf(TestClient is None, "needs the server extra: pip install -e '.[server]' httpx")
class HostSettleTest(unittest.TestCase):
    """W3 on the host: Airwallex's webhook settles its own MCP's payment, and the receipt is fetchable."""

    def setUp(self):
        from countersign.web.public import router

        environ = {"COUNTERSIGN_MERCHANT_SIGNING_KEY": base64.b64encode(bytes(range(32))).decode(),
                   "COUNTERSIGN_ACP_HMAC_SECRET": SECRET.decode(), "AIRWALLEX_WEBHOOK_SECRET": WEBHOOK_SECRET,
                   "AIRWALLEX_CLIENT_ID": "id", "AIRWALLEX_API_KEY": "key",
                   "COUNTERSIGN_PUBLIC_URL": "https://shop.example"}
        self.house = house_from_env(environ, merchant=FakeMerchant(FakeFx("84.10")))
        self.airwallex = FakeAirwallex()
        self.house.settler.client = self.airwallex
        demo = demo_app()
        demo.include_router(router(self.house))
        self.client = TestClient(Host(demo, self.house))

    def event(self, intent_id):
        raw = json.dumps({"name": "payment_intent.succeeded", "data": {"object": {"id": intent_id}}}).encode()
        headers = {"content-type": "application/json", **hmac_auth.sign(raw, WEBHOOK_SECRET.encode())}
        return self.client.post(WEBHOOK_PATH, content=raw, headers=headers)

    def test_the_webhook_settles_the_mcps_payment_and_the_receipt_is_served(self):
        api = self.house.edge.state.acp
        sku = list(self.house.merchant.catalog)[0]
        _, out = api.create_checkout({"line_items": [{"id": "line-1", "item_id": sku, "requested_quantity": 1}],
                                      "currency": "INR"})
        q = api.quote_for(out["id"])
        amount = format(from_minor(q.total_minor, "INR"), "f")
        created = q.created + timedelta(minutes=1)
        self.airwallex.intents["int_live"] = intent("int_live", q.checkout_id, amount, created=created)
        r = self.event("int_live")
        self.assertEqual(r.json(), {"order_number": q.checkout_id, "status": "paid", "receipt": True})
        served = self.client.get(f"/receipts/{q.checkout_id}")
        self.assertEqual(served.status_code, 200)
        self.assertTrue(receipt.verify(served.json(), self.house.signer.key.public_key(), q.approval).ok)
        page = self.client.get(f"/receipt/{q.checkout_id}")
        self.assertEqual(page.status_code, 200)  # the shopper's page, which fetches and checks that receipt
        self.assertIn('id="save"', page.text)
        self.assertEqual(self.client.get("/receipts/chk_unknown").status_code, 404)
        self.assertEqual(self.client.get("/receipts/k1").status_code, 404)  # only W3's receipts, by checkout id
        self.assertTrue(self.client.get("/healthz").json()["mcp_payments"])
        line = [e for e in self.house.feed._events if e["kind"] == "payment"][-1]
        self.assertEqual((line["level"], line["order"]), ("go", q.checkout_id))

    def test_only_the_success_event_settles_an_mcp_payment(self):
        # An attempt event names the same intent later; it must not run the check again.
        api = self.house.edge.state.acp
        sku = list(self.house.merchant.catalog)[0]
        _, out = api.create_checkout({"line_items": [{"id": "line-1", "item_id": sku, "requested_quantity": 1}],
                                      "currency": "EUR"})
        q = api.quote_for(out["id"])
        self.airwallex.intents["int_a"] = intent("int_a", q.checkout_id, "1.00", currency="EUR",
                                                 created=q.created + timedelta(minutes=1))
        raw = json.dumps({"name": "payment_attempt.authorization_failed",
                          "data": {"object": {"payment_intent_id": "int_a"}}}).encode()
        headers = {"content-type": "application/json", **hmac_auth.sign(raw, WEBHOOK_SECRET.encode())}
        self.assertEqual(self.client.post(WEBHOOK_PATH, content=raw, headers=headers).json(), {"ignored": True})
        self.assertEqual((self.airwallex.gets, self.airwallex.refunds), (0, []))

    def test_a_success_event_ahead_of_airwallexs_record_is_retried(self):
        # Attempt events no longer settle, so a 200 here would leave the payment unchecked for good.
        api = self.house.edge.state.acp
        sku = list(self.house.merchant.catalog)[0]
        _, out = api.create_checkout({"line_items": [{"id": "line-1", "item_id": sku, "requested_quantity": 1}],
                                      "currency": "EUR"})
        q = api.quote_for(out["id"])
        amount = format(from_minor(q.total_minor, "EUR"), "f")
        created = q.created + timedelta(minutes=1)
        self.airwallex.intents["int_slow"] = intent("int_slow", q.checkout_id, amount, currency="EUR",
                                                    status="REQUIRES_CAPTURE", created=created)
        self.assertEqual(self.event("int_slow").status_code, 503)
        self.assertEqual(self.client.get(f"/receipts/{q.checkout_id}").status_code, 404)
        self.airwallex.intents["int_slow"] = intent("int_slow", q.checkout_id, amount, currency="EUR", created=created)
        self.assertEqual(self.event("int_slow").json()["status"], "paid")
        checked = receipt.verify(self.client.get(f"/receipts/{q.checkout_id}").json(),
                                 self.house.signer.key.public_key(), q.approval)
        self.assertTrue(checked.ok)
        self.assertIn("not above the price the merchant quoted", " ".join(checked.findings))

    def test_the_receipt_page_settles_a_payment_whose_webhook_never_came(self):
        api = self.house.edge.state.acp
        sku = list(self.house.merchant.catalog)[0]
        _, out = api.create_checkout({"line_items": [{"id": "line-1", "item_id": sku, "requested_quantity": 1}],
                                      "currency": "EUR"})
        q = api.quote_for(out["id"])
        amount = format(from_minor(q.total_minor, "EUR"), "f")
        self.airwallex.intents["int_lost"] = intent("int_lost", q.checkout_id, amount, currency="EUR",
                                                    created=q.created + timedelta(minutes=1))
        served = self.client.get(f"/receipts/{q.checkout_id}")  # no webhook at all
        self.assertEqual(served.status_code, 200)
        self.assertTrue(receipt.verify(served.json(), self.house.signer.key.public_key(), q.approval).ok)
        line = [e for e in self.house.feed._events if e["kind"] == "payment"][-1]
        self.assertEqual((line["level"], line["order"]), ("go", q.checkout_id))
        gets = self.airwallex.gets
        self.assertEqual(self.event("int_lost").json()["status"], "paid")  # the late webhook changes nothing
        self.assertEqual(self.client.get(f"/receipts/{q.checkout_id}").status_code, 200)
        self.assertEqual(self.airwallex.gets, gets)

    def test_the_receipt_page_looks_up_only_our_checkouts_and_not_too_often(self):
        self.assertEqual(self.client.get("/receipts/chk_0123456789abcdef01234567").status_code, 404)
        self.assertEqual(self.airwallex.gets, 0)  # no quote of ours: Airwallex is not asked
        api = self.house.edge.state.acp
        sku = list(self.house.merchant.catalog)[0]
        _, out = api.create_checkout({"line_items": [{"id": "line-1", "item_id": sku, "requested_quantity": 1}],
                                      "currency": "EUR"})
        for _ in range(3):  # not paid yet; the page polls every 3 s
            self.assertEqual(self.client.get(f"/receipts/{out['id']}").status_code, 404)
        self.assertEqual(self.airwallex.gets, 1)

    def test_without_airwallex_keys_there_is_no_settler(self):
        house = house_from_env({"COUNTERSIGN_ACP_HMAC_SECRET": SECRET.decode()})
        self.assertIsNone(house.settler)
