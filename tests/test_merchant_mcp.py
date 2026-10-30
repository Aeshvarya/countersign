"""The fallback merchant MCP: an agent's tools over the same /acp checkout Airwallex calls."""

import base64
import json
import unittest
from decimal import Decimal

from countersign import receipt
from countersign.airwallex_merchant import AirwallexMerchant
from countersign.fx import FakeFx

try:
    from tests.test_airwallex_merchant import FakeAirwallex
except ImportError:  # run by discover with -s tests
    from test_airwallex_merchant import FakeAirwallex

try:
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from countersign.edge import hmac_auth
    from countersign.web.host import WEBHOOK_PATH, Host, house_from_env
    from countersign.web.merchant_mcp import MCP_PATH, PROTOCOL_VERSIONS, TOOLS
except ImportError:  # the server extra is not installed
    TestClient = None

TOKEN = "t" * 40
PUBLIC = "https://countersign.onrender.com"
ENV = {"COUNTERSIGN_PUBLIC_URL": PUBLIC, "COUNTERSIGN_ACP_HMAC_SECRET": "s" * 32,
       "COUNTERSIGN_MERCHANT_SIGNING_KEY": base64.b64encode(bytes(range(32))).decode(),
       "COUNTERSIGN_MCP_TOKEN": TOKEN, "AIRWALLEX_WEBHOOK_SECRET": "w" * 32}


@unittest.skipIf(TestClient is None, "needs the server extra: pip install -e '.[server]' httpx")
class MerchantMcpTest(unittest.TestCase):
    def setUp(self, merchant=None):
        self.house = house_from_env(ENV, merchant=merchant)
        self.client = TestClient(Host(FastAPI(), self.house), base_url=PUBLIC)
        self.ids = 0

    def rpc(self, method, params=None, *, token=TOKEN, headers=None):
        self.ids += 1
        body = {"jsonrpc": "2.0", "id": self.ids, "method": method, "params": params or {}}
        return self.client.post(MCP_PATH, json=body, headers={"Authorization": f"Bearer {token}", **(headers or {})})

    def tool(self, name, **args):
        r = self.rpc("tools/call", {"name": name, "arguments": args})
        self.assertEqual(r.status_code, 200)
        return r.json()["result"]

    def checkout(self, **extra):
        sku = self.house.merchant.search("kettle")[0].sku
        out = self.tool("create_checkout", items=[{"id": sku, "quantity": 1}], currency="INR", **extra)
        self.assertFalse(out["isError"], out["content"][0]["text"])
        return sku, out["structuredContent"]["checkout"]

    def approval_of(self, sid):
        return self.house.edge.state.acp.sessions[sid].approval

    # ------------------------------------------------------------ the door

    def test_no_token_no_entry(self):
        self.assertEqual(self.rpc("tools/list", token="wrong" * 8).status_code, 401)
        r = self.client.post(MCP_PATH, json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"})
        self.assertEqual((r.status_code, r.headers["www-authenticate"]), (401, "Bearer"))

    def test_another_origin_is_refused(self):
        self.assertEqual(self.rpc("ping", headers={"Origin": "https://evil.example"}).status_code, 403)
        self.assertEqual(self.rpc("ping", headers={"Origin": PUBLIC}).status_code, 200)

    def test_without_a_token_the_door_is_not_there(self):
        house = house_from_env({k: v for k, v in ENV.items() if k != "COUNTERSIGN_MCP_TOKEN"})
        client = TestClient(Host(FastAPI(), house))
        r = client.post(MCP_PATH, json={"jsonrpc": "2.0", "id": 1, "method": "ping"},
                        headers={"Authorization": f"Bearer {TOKEN}"})
        self.assertEqual(r.status_code, 404)

    def test_bad_configuration_stops_the_service(self):
        with self.assertRaises(ValueError):
            house_from_env({**ENV, "COUNTERSIGN_MCP_TOKEN": "short"})
        with self.assertRaises(ValueError):  # the MCP is a caller of /acp, so /acp must be there
            house_from_env({k: v for k, v in ENV.items() if k != "COUNTERSIGN_ACP_HMAC_SECRET"})

    def test_initialize_and_list(self):
        r = self.rpc("initialize", {"protocolVersion": "2025-06-18", "capabilities": {},
                                    "clientInfo": {"name": "claude-code", "version": "2"}}).json()["result"]
        self.assertEqual((r["protocolVersion"], r["capabilities"]), ("2025-06-18", {"tools": {"listChanged": False}}))
        self.assertIn("clear yes", r["instructions"])
        old = self.rpc("initialize", {"protocolVersion": "1999-01-01"}).json()["result"]
        self.assertEqual(old["protocolVersion"], PROTOCOL_VERSIONS[0])
        names = [t["name"] for t in self.rpc("tools/list").json()["result"]["tools"]]
        self.assertEqual(names, [t["name"] for t in TOOLS])
        self.assertIn("complete_checkout", names)

    def test_protocol_edges(self):
        note = self.client.post(MCP_PATH, json={"jsonrpc": "2.0", "method": "notifications/initialized"},
                                headers={"Authorization": f"Bearer {TOKEN}"})
        self.assertEqual(note.status_code, 202)
        self.assertEqual(self.rpc("resources/list").json()["error"]["code"], -32601)
        self.assertEqual(self.rpc("tools/call", {"name": "pay_anything"}).json()["error"]["code"], -32602)
        self.assertEqual(self.rpc("ping", headers={"MCP-Protocol-Version": "1999-01-01"}).status_code, 400)
        self.assertEqual(self.client.get(MCP_PATH, headers={"Authorization": f"Bearer {TOKEN}"}).status_code, 405)
        bad = self.client.post(MCP_PATH, content=b"[1,2]", headers={"Authorization": f"Bearer {TOKEN}",
                                                                    "content-type": "application/json"})
        self.assertEqual(bad.json()["error"]["code"], -32600)

    # ------------------------------------------------------------ the tools

    def test_search_links_the_shop_window(self):
        out = self.tool("search_products", query="kettle", limit=2)["structuredContent"]["products"]
        self.assertEqual(len(out), 2)
        self.assertEqual(out[0]["link"], f"{PUBLIC}/p/{out[0]['id']}")
        self.assertTrue(out[0]["price"].endswith(" USD"))
        self.assertTrue(self.tool("search_products", query="kettle", limit=99)["isError"])

    def test_create_then_complete_pays_once_with_a_receipt_anyone_can_verify(self):
        _, view = self.checkout()
        self.assertEqual((view["status"], view["currency"]), ("ready_for_payment", "inr"))
        done = self.tool("complete_checkout", checkout_session_id=view["id"])
        self.assertIn("payment paid, receipt signed", done["content"][0]["text"])
        pay = done["structuredContent"]["payment"]
        public = self.house.signer.key.public_key()
        self.assertTrue(receipt.verify(pay["receipt"], public, self.approval_of(view["id"])).ok)
        again = self.tool("complete_checkout", checkout_session_id=view["id"])["structuredContent"]["payment"]
        self.assertEqual(again["order_number"], pay["order_number"])
        self.assertEqual(len(self.house.merchant.orders), 1)

    def test_a_price_rise_after_the_shopper_looked_charges_nothing(self):
        sku, view = self.checkout()
        self.house.merchant.set_price(sku, self.house.merchant.catalog[sku].price + Decimal("5"))
        out = self.tool("complete_checkout", checkout_session_id=view["id"])
        msgs = out["structuredContent"]["checkout"]["messages"]
        self.assertTrue(any(m.get("resolution") == "requires_buyer_review" for m in msgs))
        self.assertIsNone(out["structuredContent"]["payment"])
        self.assertEqual(self.house.merchant.orders, {})
        self.assertIn("Nothing was charged", out["content"][0]["text"])

    def test_a_ceiling_in_the_shoppers_currency(self):
        _, view = self.checkout(ceiling="99999.00")
        self.assertEqual(view["countersign"]["ceiling"], 9999900)
        self.assertTrue(self.tool("create_checkout", items=[{"id": "x", "quantity": 1}], currency="INR",
                                  ceiling="1.001")["isError"])
        _, view = self.checkout()
        up = self.tool("update_checkout", checkout_session_id=view["id"], ceiling="99999")["structuredContent"]
        self.assertEqual(up["checkout"]["countersign"]["ceiling"], 9999900)

    def test_cancel_and_bad_ids(self):
        _, view = self.checkout()
        self.assertEqual(self.tool("cancel_checkout", checkout_session_id=view["id"])
                         ["structuredContent"]["checkout"]["status"], "canceled")
        self.assertTrue(self.tool("complete_checkout", checkout_session_id=view["id"])["isError"])
        self.assertTrue(self.tool("get_checkout", checkout_session_id="../../orders/k1")["isError"])
        self.assertTrue(self.tool("get_checkout", checkout_session_id="cs_" + "0" * 24)["isError"])


@unittest.skipIf(TestClient is None, "needs the server extra: pip install -e '.[server]' httpx")
class MerchantMcpCardTest(MerchantMcpTest):
    """With real card payments the order waits for Airwallex, and the receipt comes after the webhook."""

    def setUp(self):
        self.api = FakeAirwallex()
        super().setUp(AirwallexMerchant(self.api, FakeFx("84.10")))

    def test_create_then_complete_pays_once_with_a_receipt_anyone_can_verify(self):
        _, view = self.checkout()
        pay = self.tool("complete_checkout", checkout_session_id=view["id"])["structuredContent"]["payment"]
        self.assertEqual((pay["status"], pay["receipt"]), ("pending", None))

        key = f"acp:{view['id']}"
        order = self.house.merchant.orders[key]
        decision, _ = self.house.merchant._decisions[key]
        self.api.shopper_pays(order.payment_id, decision.charge, quote=decision.fx_quote_id)
        raw = json.dumps({"name": "payment_intent.succeeded", "data": {"object": {"id": order.payment_id}}}).encode()
        self.client.post(WEBHOOK_PATH, content=raw, headers=hmac_auth.sign(raw, ENV["AIRWALLEX_WEBHOOK_SECRET"].encode()))

        out = self.tool("get_checkout", checkout_session_id=view["id"])
        pay = out["structuredContent"]["payment"]
        self.assertEqual(pay["status"], "paid")
        public = self.house.signer.key.public_key()
        self.assertTrue(receipt.verify(pay["receipt"], public, self.approval_of(view["id"])).ok)


if __name__ == "__main__":
    unittest.main()
