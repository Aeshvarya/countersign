"""A16: a declined card through the ACP door (Kit 10 R7, no duplicate payment).

The card is refused in Airwallex's Drop-in, after complete has started the
payment. The session must then say so (payment_declined), charge nothing, sign
nothing, start no second intent when the agent completes again, and still let
the shopper pay the same order with another card.
"""

import unittest

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from countersign import receipt, tap
from countersign.airwallex import AirwallexError
from countersign.airwallex_merchant import AirwallexMerchant
from countersign.checkout import release
from countersign.fx import FakeFx

try:
    from tests.test_airwallex_merchant import FakeAirwallex
except ImportError:
    from test_airwallex_merchant import FakeAirwallex

try:
    from fastapi.testclient import TestClient

    from countersign.edge import create_app
    from countersign.edge.acp import AcpConfig

    try:
        from tests import test_acp as acp_tests  # the module, not its classes: unittest would run them here too
    except ImportError:
        import test_acp as acp_tests
    SECRET, SESSIONS = acp_tests.SECRET, acp_tests.SESSIONS
except ImportError:  # the server extra is not installed
    TestClient = None

PUBLIC = "https://shop.example"


@unittest.skipIf(TestClient is None, "needs the server extra: pip install -e '.[server]' httpx")
class DeclinedCardTest(unittest.TestCase):
    if TestClient is not None:
        config = AcpConfig(secret=SECRET, public_url=PUBLIC)
        call, create, complete = acp_tests.AcpTest.call, acp_tests.AcpTest.create, acp_tests.AcpTest.complete

    def setUp(self):
        self.clock = acp_tests.Clock()
        self.api = FakeAirwallex()
        self.merchant = AirwallexMerchant(self.api, FakeFx("84.10"))
        self.key = Ed25519PrivateKey.generate()
        self.signer = receipt.MerchantSigner(self.key, "m-1")
        self.app = create_app(self.merchant, tap.StaticDirectory({}), authority="testserver", clock=self.clock,
                              signer=self.signer, acp=self.config)
        self.client = TestClient(self.app)
        self.sku = self.merchant.search("kettle")[0].sku
        self.stock = self.merchant.catalog[self.sku].stock
        self.keys = 0

    def started(self):
        """A session completed by the agent: the order waits for the shopper in Drop-in."""
        sid = self.create()["id"]
        r = self.complete(sid)
        self.assertEqual((r.status_code, r.json()["status"]), (200, "completed"), r.text)
        return sid, self.merchant.orders[f"acp:{sid}"]

    def declined(self, code="authorization_failed"):
        sid, order = self.started()
        self.api.card_declined(order.payment_id, code)
        self.merchant.confirm(order.payment_id)  # the payment_attempt webhook, or the shopper page's check
        return sid, order

    def view(self, sid):
        r = self.call("GET", f"{SESSIONS}/{sid}")
        self.assertEqual(r.status_code, 200, r.text)
        return r.json()

    @staticmethod
    def codes(view):
        return [m.get("code") for m in view["messages"]]

    def test_a_declined_card_reads_as_payment_declined_and_charges_nothing(self):
        sid, order = self.declined()
        view = self.view(sid)
        self.assertEqual(view["status"], "not_ready_for_payment")
        self.assertEqual(self.codes(view), ["payment_declined"])
        message = view["messages"][0]
        self.assertEqual(message["type"], "error")
        self.assertIn("Nothing was charged", message["content"])
        self.assertIn(f"{PUBLIC}/pay/{sid}", message["content"])  # where the shopper tries another card
        self.assertEqual(view["countersign"]["payment_failure"], "authorization_failed")
        self.assertIsNone(view["countersign"]["receipt"])
        self.assertEqual(view["order"]["permalink_url"], f"{PUBLIC}/pay/{sid}")
        self.assertEqual(self.merchant.orders[f"acp:{sid}"].status, "pending")  # not paid, not closed
        self.assertNotIn("SUCCEEDED", [i["status"] for i in self.api.intents.values()])
        self.assertEqual(self.api.refunds, [])

    def test_completing_again_after_a_decline_starts_no_second_intent(self):
        sid, _ = self.declined()
        for _ in range(2):  # the agent retries with fresh idempotency keys
            r = self.complete(sid)
            self.assertEqual(r.status_code, 200, r.text)
            self.assertEqual((r.json()["status"], self.codes(r.json())), ("not_ready_for_payment", ["payment_declined"]))
        self.assertEqual((self.api.creates, len(self.api.intents)), (1, 1))
        self.assertEqual(self.api.posts, ["/pa/payment_intents/create"])

    def test_another_card_after_a_decline_is_one_order_one_charge_one_receipt(self):
        sid, order = self.declined()
        key = f"acp:{sid}"
        decision, _ = self.merchant._decisions[key]
        self.api.shopper_pays(order.payment_id, decision.charge, quote=decision.fx_quote_id)
        paid = self.merchant.confirm(order.payment_id)
        self.assertEqual(paid.status, "paid")
        signed = release(paid, self.signer, self.app.state.acp.store)
        self.assertIsNotNone(signed)
        view = self.view(sid)
        self.assertEqual((view["status"], view["messages"]), ("completed", []))
        self.assertNotIn("payment_failure", view["countersign"])
        self.assertEqual(view["countersign"]["receipt"], signed)
        self.assertEqual((len(self.merchant.orders), self.api.creates, self.api.refunds), (1, 1, []))

    def test_a_receipt_signed_after_the_card_pays_reaches_the_session(self):
        # Airwallex's hosted MCP sees only this session: the receipt the webhook signs must show up in it.
        sid, order = self.started()
        self.assertIsNone(self.view(sid)["countersign"]["receipt"])  # paying in Drop-in: nothing signed yet
        decision, _ = self.merchant._decisions[f"acp:{sid}"]
        self.api.shopper_pays(order.payment_id, decision.charge, quote=decision.fx_quote_id)
        signed = release(self.merchant.confirm(order.payment_id), self.signer, self.app.state.acp.store)
        self.assertEqual(self.view(sid)["countersign"]["receipt"], signed)
        self.assertTrue(receipt.verify(signed, self.key.public_key()).ok)

    def test_an_outage_is_never_reported_as_a_declined_card(self):
        sid, _ = self.declined("provider_unavailable")
        view = self.view(sid)
        self.assertEqual((view["status"], self.codes(view)), ("not_ready_for_payment", ["payment_unavailable"]))
        content = view["messages"][0]["content"]
        self.assertIn("provider_unavailable", content)
        self.assertNotIn("card was declined", content)
        self.assertIn("Nothing was charged", content)

    # -- review fix 1: never assert a decline, or its absence, without a code that says so

    def test_a_failed_attempt_with_no_code_is_reported_neutrally(self):
        sid, _ = self.declined(code=None)
        view = self.view(sid)
        self.assertEqual((view["status"], self.codes(view)), ("not_ready_for_payment", ["payment_failed"]))
        content = view["messages"][0]["content"]
        self.assertIn("did not go through", content)
        self.assertIn("Airwallex gave no reason", content)
        self.assertNotIn("declined", content.lower())
        self.assertIn(f"{PUBLIC}/pay/{sid}", content)
        self.assertIsNone(view["countersign"]["payment_failure"])

    def test_capture_failed_says_what_airwallex_reported_and_nothing_more(self):
        sid, _ = self.declined("capture_failed")
        view = self.view(sid)
        self.assertEqual(self.codes(view), ["payment_failed"])
        content = view["messages"][0]["content"]
        self.assertIn("capture_failed", content)
        self.assertNotIn("declined", content.lower())
        self.assertEqual(view["countersign"]["payment_failure"], "capture_failed")

    # -- review fix 4: update stays refused, and says why

    def test_update_after_a_decline_says_where_the_shopper_pays_or_to_cancel(self):
        sid, _ = self.declined()
        r = self.call("POST", f"{SESSIONS}/{sid}", {"items": [{"id": self.sku, "quantity": 2}]})
        self.assertEqual(r.status_code, 405, r.text)
        body = r.json()
        self.assertEqual(body["code"], "payment_declined")
        self.assertIn("The card was declined", body["message"])
        self.assertIn(f"{PUBLIC}/pay/{sid}", body["message"])
        self.assertIn("cancel this checkout", body["message"])
        self.assertNotIn("the session is completed", body["message"])

    def test_the_shopper_can_cancel_after_a_decline(self):
        sid, order = self.declined()
        r = self.call("POST", f"{SESSIONS}/{sid}/cancel", {})
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual((r.json()["status"], r.json()["messages"]), ("canceled", []))
        self.assertNotIn("order", r.json())
        self.assertEqual(self.merchant.orders[f"acp:{sid}"].status, "failed")
        self.assertEqual(self.api.intents[order.payment_id]["status"], "CANCELLED")
        self.assertEqual(self.merchant.catalog[self.sku].stock, self.stock)

    def test_a_cancel_airwallex_cannot_answer_changes_nothing(self):
        sid, order = self.declined()

        def down(*args, **kwargs):
            raise AirwallexError(503, "unavailable")

        self.api.post = self.api.get = down
        r = self.call("POST", f"{SESSIONS}/{sid}/cancel", {})
        body = r.json()
        self.assertEqual((r.status_code, body["type"], body["code"]),
                         (503, "service_unavailable", "payment_service_unavailable"))
        self.assertEqual(r.headers.get("retry-after"), "5")
        self.assertNotIn("charged", body["message"])  # we cannot know; the cancel may have reached Airwallex
        self.assertEqual(self.merchant.orders[f"acp:{sid}"].status, "pending")
        self.assertEqual(self.codes(self.app.state.acp.view(self.app.state.acp.sessions[sid])), ["payment_declined"])

    # -- review fix 2: a cancel that loses to a paid card answers paid, with its receipt

    def pays_now(self, sid, order, extra=0):
        """Another card pays in Drop-in; nothing has told us yet (no webhook, no page check)."""
        decision, _ = self.merchant._decisions[f"acp:{sid}"]
        self.api.shopper_pays(order.payment_id, decision.charge + extra, quote=decision.fx_quote_id)

    def test_a_cancel_after_another_card_paid_answers_paid_with_its_receipt(self):
        sid, order = self.declined()
        self.pays_now(sid, order)
        r = self.call("POST", f"{SESSIONS}/{sid}/cancel", {})
        self.assertEqual(r.status_code, 200, r.text)
        view = r.json()
        self.assertEqual((view["status"], view["messages"]), ("completed", []))
        self.assertEqual(view["order"]["id"], order.order_number)
        signed = view["countersign"]["receipt"]
        approval = self.app.state.acp.sessions[sid].approval
        self.assertTrue(receipt.verify(signed, self.key.public_key(), approval).ok)
        self.assertEqual(self.app.state.acp.store.get(f"acp:{sid}"), signed)  # the same receipt the webhook would sign
        self.assertEqual(self.merchant.orders[f"acp:{sid}"].status, "paid")

    # -- review fix 3: any unpaid open card payment can be canceled, decline or not

    def test_an_open_card_payment_can_be_canceled_before_any_card_is_tried(self):
        sid, order = self.started()
        r = self.call("POST", f"{SESSIONS}/{sid}/cancel", {})
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual((r.json()["status"], r.json()["messages"]), ("canceled", []))
        self.assertEqual(self.api.intents[order.payment_id]["status"], "CANCELLED")
        self.assertEqual(self.merchant.orders[f"acp:{sid}"].status, "failed")
        self.assertEqual(self.merchant.catalog[self.sku].stock, self.stock)

    def test_a_cancel_that_finds_the_open_payment_paid_keeps_it_paid(self):
        sid, order = self.started()  # paid in Drop-in, the webhook not here yet
        self.pays_now(sid, order)
        r = self.call("POST", f"{SESSIONS}/{sid}/cancel", {})
        self.assertEqual((r.status_code, r.json()["status"]), (200, "completed"))
        self.assertIsNotNone(r.json()["countersign"]["receipt"])
        self.assertEqual(self.api.refunds, [])

    # -- review fix 5: a refund found while canceling is said, never a clean cancel

    def test_a_cancel_that_meets_a_refunded_payment_says_a_payment_was_refunded(self):
        sid, order = self.declined()
        self.app.state.acp.sessions[sid].receipt = {"stale": True}
        self.pays_now(sid, order, extra=500)  # a card paid the wrong amount
        r = self.call("POST", f"{SESSIONS}/{sid}/cancel", {})
        self.assertEqual(r.status_code, 200, r.text)
        view = r.json()
        self.assertEqual((view["status"], self.codes(view)), ("canceled", ["payment_refunded"]))
        self.assertIn("refunded in full", view["messages"][0]["content"])
        self.assertNotIn("order", view)
        self.assertIsNone(self.app.state.acp.sessions[sid].receipt)
        self.assertEqual([x["payment_intent_id"] for x in self.api.refunds], [order.payment_id])

    def test_a_paid_session_still_cannot_be_canceled(self):
        sid, order = self.declined()
        decision, _ = self.merchant._decisions[f"acp:{sid}"]
        self.api.shopper_pays(order.payment_id, decision.charge, quote=decision.fx_quote_id)
        self.merchant.confirm(order.payment_id)
        self.assertEqual(self.call("POST", f"{SESSIONS}/{sid}/cancel", {}).status_code, 405)

    def test_a_payment_still_open_without_a_decline_is_unchanged(self):
        sid, _ = self.started()
        view = self.view(sid)
        self.assertEqual((view["status"], view["messages"]), ("completed", []))
        self.assertNotIn("payment_failure", view["countersign"])


if __name__ == "__main__":
    unittest.main()
