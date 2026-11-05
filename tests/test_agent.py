import json
import unittest
from datetime import datetime, timedelta, timezone
from decimal import Decimal

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from countersign import receipt, tap
from countersign.agent import AgentKey, AgentSession, LocalShop, ScriptedModel
from countersign.agent.session import TOOLS
from countersign.fx import FakeFx
from countersign.merchant import FakeMerchant


class Clock:
    def __init__(self):
        self.now = datetime(2026, 11, 2, 10, 0, tzinfo=timezone.utc)

    def __call__(self):
        return self.now


def build(rate="84.10"):
    clock = Clock()
    fx = FakeFx(rate)
    shop = FakeMerchant(fx)
    agent_key = AgentKey(Ed25519PrivateKey.generate(), "agent-key-1", "https://agents.example")
    merchant_key = Ed25519PrivateKey.generate()
    local = LocalShop(shop, agent_key, clock, signer=receipt.MerchantSigner(merchant_key, "m-1"))
    session = AgentSession(local, ScriptedModel(), "shopper@example.com", "INR", clock=clock)
    return session, shop, fx, clock, local, merchant_key


class ToolSchemaTest(unittest.TestCase):
    def test_every_tool_is_strict_and_closed(self):
        def closed(schema):
            if schema.get("type") == "object":
                self.assertFalse(schema["additionalProperties"])
                self.assertEqual(set(schema["required"]), set(schema["properties"]))
                for sub in schema["properties"].values():
                    closed(sub)
            if schema.get("type") == "array":
                closed(schema["items"])

        for tool in TOOLS:
            with self.subTest(tool["name"]):
                self.assertTrue(tool["strict"])
                closed(tool["input_schema"])

    def test_no_tool_takes_an_amount(self):
        text = json.dumps([t["input_schema"] for t in TOOLS]).lower()
        for word in ("amount", "price", "total", "ceiling", "rate"):
            self.assertNotIn(f'"{word}"', text)


class AgentFlowTest(unittest.TestCase):
    def test_happy_path_pauses_for_approval_then_pays_with_a_receipt(self):
        session, shop, fx, clock, local, merchant_key = build()
        session.start("buy me a mechanical keyboard")
        self.assertEqual(session.status, "awaiting_approval")
        self.assertEqual(shop.orders, {})  # nothing charged before approval

        session.approve(session.suggested_ceiling())
        self.assertEqual(session.status, "done")
        self.assertEqual(len(shop.orders), 1)
        self.assertEqual(len(session.receipts), 1)
        self.assertTrue(receipt.verify(session.receipts[0], merchant_key.public_key()).ok)
        self.assertTrue(all(e["ok"] for e in local.edge_log))
        self.assertEqual({e["tag"] for e in local.edge_log}, {tap.BROWSE, tap.PAY})

    def test_rate_moves_past_the_ceiling_so_the_agent_asks_again_then_pays(self):
        session, shop, fx, clock, *_ = build()
        session.start("buy me a mechanical keyboard")
        session.approve()  # ceiling = the shown total exactly
        # The first checkout used the same rate, so it proceeded. Run a second purchase
        # where the market moves between approval and checkout.
        session2, shop2, fx2, clock2, *_ = build()
        session2.start("buy me a mechanical keyboard")
        fx2.rate = Decimal("88.40")
        session2.approve()
        self.assertEqual(session2.status, "awaiting_approval")  # asked again
        kinds = [e["kind"] for e in session2.events]
        self.assertIn("decision", kinds)
        reask = next(e for e in session2.events if e["kind"] == "decision")
        self.assertEqual(reask["data"]["decision"]["outcome"], "reask")
        self.assertEqual(shop2.orders, {})
        session2.approve(session2.suggested_ceiling())
        self.assertEqual(session2.status, "done")
        self.assertEqual(len(shop2.orders), 1)

    def test_a_product_that_only_shares_a_word_is_not_offered(self):
        # The catalogue has no monitors, only monitor arms: an arm is not what was asked for.
        session, shop, *_ = build()
        session.start("Buy me a monitor under ₹12,000")
        self.assertEqual(session.status, "done")
        self.assertIsNone(session.pending)
        said = " ".join(e["text"] or "" for e in session.events)
        self.assertIn("doesn't sell what you asked for", said)
        self.assertIn("Monitor arm", said)
        self.assertEqual(shop.orders, {})

    def test_asking_for_the_arm_by_name_finds_it(self):
        session, shop, *_ = build()
        session.start("buy me a monitor arm")
        self.assertEqual(session.status, "awaiting_approval")
        self.assertEqual(session.offers[session.pending.offer_id].items[0].title, "Monitor arm Lite")

    def test_plural_requests_match_singular_names(self):
        session, *_ = build()
        session.start("buy me headphones")
        self.assertEqual(session.status, "awaiting_approval")

    def test_decline_charges_nothing(self):
        session, shop, *_ = build()
        session.start("buy me a kettle")
        session.decline()
        self.assertEqual(session.status, "done")
        self.assertEqual(shop.orders, {})

    def test_checkout_without_approval_is_refused_by_the_session(self):
        session, shop, *_ = build()
        out = session._checkout({"offer_id": "off_9"})
        self.assertEqual(out["outcome"], "refuse")
        self.assertEqual(shop.orders, {})

    def test_an_agent_whose_key_the_merchant_does_not_know_is_refused_at_the_edge(self):
        session, shop, fx, clock, local, _ = build()
        local.directory = tap.StaticDirectory({})
        session.start("buy me a kettle")
        self.assertEqual(shop.orders, {})
        self.assertFalse(local.edge_log[0]["ok"])

    def test_approval_screen_numbers_come_from_the_offer_not_the_model(self):
        session, *_ = build()
        session.start("buy me a backpack")
        event = next(e for e in session.events if e["kind"] == "approval")
        offer = session.offers[session.pending.offer_id]
        self.assertEqual(event["data"]["offer"]["shopper_total"], format(offer.shopper_total, "f"))


if __name__ == "__main__":
    unittest.main()
