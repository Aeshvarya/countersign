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
        self.signer = receipt.MerchantSigner(Ed25519PrivateKey.generate(), "m-1")
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
        self.assertEqual((len(self.merchant.orders), self.api.creates, self.api.refunds), (1, 1, []))

    def test_an_outage_is_never_reported_as_a_declined_card(self):
        sid, _ = self.declined("provider_unavailable")
        view = self.view(sid)
        self.assertEqual((view["status"], self.codes(view)), ("not_ready_for_payment", ["payment_unavailable"]))
        self.assertNotIn("declined", view["messages"][0]["content"].lower().replace("not a declined card", ""))
        self.assertIn("Nothing was charged", view["messages"][0]["content"])

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
        self.assertEqual((r.status_code, r.json()["code"]), (503, "service_unavailable"))
        self.assertEqual(self.merchant.orders[f"acp:{sid}"].status, "pending")
        self.assertEqual(self.codes(self.app.state.acp.view(self.app.state.acp.sessions[sid])), ["payment_declined"])

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
