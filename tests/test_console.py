"""A15 and V6: the merchant console's live feed, its state, and the levers that change the catalogue on cue."""

import base64
import hashlib
import json
import unittest
from datetime import datetime, timedelta, timezone
from decimal import Decimal

from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from countersign import catalog_gmc, tap
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
    from countersign.edge.passkey import b64url
    from countersign.web import console, levers, shopper
    from countersign.web.host import WEBHOOK_PATH, Host, house_from_env
    from countersign.web.merchant_mcp import MCP_PATH
except ImportError:  # the server extra is not installed
    TestClient = None

PUBLIC = "https://countersign.onrender.com"
SECRET = "s" * 32
LEVER = "L" * 40
MCP = "m" * 40
SESSIONS = "/acp/checkout_sessions"
AGENT_DIR = "https://agents.example"
ENV = {"COUNTERSIGN_PUBLIC_URL": PUBLIC, "COUNTERSIGN_ACP_HMAC_SECRET": SECRET,
       "COUNTERSIGN_MERCHANT_SIGNING_KEY": base64.b64encode(bytes(range(32))).decode(),
       "COUNTERSIGN_REQUIRE_SHOPPER_SIGNATURE": "1", "COUNTERSIGN_LEVER_TOKEN": LEVER,
       "COUNTERSIGN_MCP_TOKEN": MCP, "AIRWALLEX_WEBHOOK_SECRET": "w" * 32}


@unittest.skipIf(TestClient is None, "needs the server extra")
class FeedTest(unittest.TestCase):
    def setUp(self):
        self.now = [datetime(2026, 11, 2, 10, 0, tzinfo=timezone.utc)]
        self.feed = console.Feed(size=3, clock=lambda: self.now[0])

    def test_numbered_and_bounded(self):
        for i in range(5):
            self.feed.add("acp", "info", f"event {i}")
        events = self.feed.since(0)
        self.assertEqual([e["title"] for e in events], ["event 2", "event 3", "event 4"])
        self.assertEqual(self.feed.since(4)[0]["title"], "event 4")
        self.assertEqual(self.feed.seq, 5)

    def test_a_quick_repeat_is_counted_on_the_same_line(self):
        first = self.feed.add("passkey", "info", "Shopper opened the approval page", session="cs_1")
        self.now[0] += timedelta(seconds=3)
        again = self.feed.add("passkey", "info", "Shopper opened the approval page", session="cs_1")
        self.assertEqual((again["id"], again["count"]), (first["id"], 2))
        self.assertEqual(len(self.feed.since(0)), 1)
        self.assertEqual(self.feed.since(1)[0]["count"], 2)  # a client that saw the first line gets the update
        self.now[0] += timedelta(seconds=console.DEDUPE_SECONDS + 1)
        self.assertNotEqual(self.feed.add("passkey", "info", "Shopper opened the approval page", session="cs_1")["id"],
                            first["id"])


class HouseCase(unittest.TestCase):
    def merchant(self):
        return None

    def build(self, **env):
        self.agent = Ed25519PrivateKey.generate()
        directory = tap.StaticDirectory({(AGENT_DIR, "k1"): self.agent.public_key()})
        self.house = house_from_env({**ENV, **env}, merchant=self.merchant(), directory=directory)
        app = FastAPI()
        app.include_router(shopper.router(self.house))
        if self.house.lever_token:
            app.include_router(console.router(self.house))
        self.client = TestClient(Host(app, self.house), base_url=PUBLIC)
        self.keys = 0
        self.device = ec.generate_private_key(ec.SECP256R1())

    def setUp(self):
        self.build()

    def acp(self, path, body, *, signed=True):
        self.keys += 1
        raw = json.dumps(body).encode()
        headers = {"content-type": "application/json", "Idempotency-Key": f"c-{self.keys}"}
        if signed:
            headers.update(hmac_auth.sign(raw, SECRET.encode()))
        return self.client.post(path, content=raw, headers=headers)

    def session(self):
        self.sku = self.house.merchant.search("kettle")[0].sku
        return self.acp(SESSIONS, {"items": [{"id": self.sku, "quantity": 1}], "currency": "INR"}).json()

    def sign(self, sid):
        data = self.client.get(f"/acp/approvals/{sid}").json()
        ad = hashlib.sha256(b"countersign.onrender.com").digest() + b"\x05" + (1).to_bytes(4, "big")
        cdj = json.dumps({"type": "webauthn.get", "challenge": data["webauthn"]["challenge"],
                          "origin": PUBLIC}).encode()
        sig = self.device.sign(ad + hashlib.sha256(cdj).digest(), ec.ECDSA(hashes.SHA256()))
        spki = self.device.public_key().public_bytes(serialization.Encoding.DER,
                                                     serialization.PublicFormat.SubjectPublicKeyInfo)
        r = self.client.post(f"/acp/approvals/{sid}", json={
            "credential_id": b64url(b"d1"), "public_key_spki_der": b64url(spki), "authenticator_data": b64url(ad),
            "client_data_json": b64url(cdj), "signature": b64url(sig)})
        self.assertEqual(r.status_code, 200, r.text)

    def titles(self):
        return [e["title"] for e in self.house.feed.since(0)]

    def auth(self, token=LEVER):
        return {"Authorization": f"Bearer {token}"}


@unittest.skipIf(TestClient is None, "needs the server extra")
class FeedRecordingTest(HouseCase):
    def test_the_whole_story_reads_in_order(self):
        s = self.session()
        self.acp(f"{SESSIONS}/{s['id']}/complete", {})
        self.sign(s["id"])
        levers.raise_price(self.house, self.sku, 12)       # the merchant moves the price after approval
        self.acp(f"{SESSIONS}/{s['id']}/complete", {})      # asked again; nothing charged
        self.sign(s["id"])                                  # the shopper approves the new terms
        done = self.acp(f"{SESSIONS}/{s['id']}/complete", {}).json()
        titles = self.titles()
        self.assertEqual(titles[0], "Checkout created: 1 item")
        self.assertIn("Waiting for the shopper's passkey; nothing charged", titles)
        self.assertIn("A passkey signed the exact terms", titles)
        self.assertTrue(any(t.startswith("Merchant changed price") for t in titles))
        self.assertIn("Asked the shopper again: the price changed; nothing charged", titles)
        self.assertEqual(titles[-1], "Countersigned: the terms still match, order placed")
        last = self.house.feed.since(0)[-1]
        self.assertEqual((last["level"], last["order"], last["door"]), ("go", done["order"]["id"], "platform"))

    def test_refusals_show_up(self):
        r = self.acp(SESSIONS, {"items": []}, signed=False)
        self.assertEqual(r.status_code, 401)
        self.client.post("/checkout", json={"approval": {}, "idempotency_key": "x"})  # no TAP signature
        events = self.house.feed.since(0)
        self.assertEqual([(e["kind"], e["level"]) for e in events], [("acp", "no"), ("tap", "no")])
        self.assertEqual(events[0]["title"], "Refused a checkout call: bad platform signature")

    def test_request_bodies_never_reach_the_feed(self):
        s = self.session()
        self.sign(s["id"])
        card = "4035501000000008"
        self.acp(f"{SESSIONS}/{s['id']}/complete",
                 {"payment_data": {"card_number": card, "cvc": "737", "token": "tok_secret_123"},
                  "buyer": {"email": "shopper@example.com"}})
        dump = json.dumps(self.house.feed.since(0))
        for secret in (card, "tok_secret_123", "shopper@example.com"):
            self.assertNotIn(secret, dump)
        r = self.client.get("/api/console/state", headers=self.auth())
        self.assertNotIn(card, r.text)

    def test_our_mcp_calls_are_labelled(self):
        sku = self.house.merchant.search("kettle")[0].sku
        body = {"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                "params": {"name": "create_checkout", "arguments": {"items": [{"id": sku, "quantity": 1}],
                                                                    "currency": "INR"}}}
        r = self.client.post(MCP_PATH, json=body, headers={"Authorization": f"Bearer {MCP}"})
        self.assertEqual(r.status_code, 200)
        self.assertEqual(self.house.feed.since(0)[-1]["door"], "our MCP")


@unittest.skipIf(TestClient is None, "needs the server extra")
class ConsoleApiTest(HouseCase):
    def test_token_required(self):
        self.assertEqual(self.client.get("/api/console/state").status_code, 401)
        self.assertEqual(self.client.get("/api/console/state", headers=self.auth("x" * 40)).status_code, 401)
        self.assertEqual(self.client.post("/api/levers/stock", json={"sku": "x", "stock": 0}).status_code, 401)
        self.assertEqual(self.client.get("/api/console/feed", headers=self.auth()).status_code, 200)

    def test_without_a_token_the_console_does_not_exist(self):
        from countersign.web.app import HOUSE, app
        self.assertEqual(HOUSE.lever_token, b"")
        client = TestClient(app)
        self.assertEqual(client.get("/api/console/state", headers=self.auth()).status_code, 404)
        self.assertEqual(client.post("/api/levers/stock", headers=self.auth(), json={}).status_code, 404)

    def test_state_lists_checkouts_and_orders(self):
        s = self.session()
        self.sign(s["id"])
        done = self.acp(f"{SESSIONS}/{s['id']}/complete", {}).json()
        st = self.client.get("/api/console/state", headers=self.auth()).json()
        self.assertEqual(st["sessions"][0]["id"], s["id"])
        self.assertEqual(st["sessions"][0]["order"], done["order"]["id"])
        self.assertEqual(st["orders"][0]["order_number"], done["order"]["id"])
        self.assertIsNotNone(st["orders"][0]["receipt"])
        self.assertTrue(st["config"]["shopper_passkey"])
        self.assertTrue(st["config"]["fallback_mcp"])

    def test_products_include_what_is_out_of_stock(self):
        sku = self.house.merchant.search("kettle")[0].sku
        levers.set_stock(self.house, sku, 0)
        found = self.client.get("/api/console/products", params={"q": sku}, headers=self.auth()).json()["products"]
        self.assertEqual((found[0]["id"], found[0]["stock"]), (sku, 0))

    def test_lever_routes(self):
        sku = self.house.merchant.search("kettle")[0].sku
        before = self.house.merchant.catalog[sku].price
        r = self.client.post("/api/levers/price", json={"sku": sku, "percent": 12}, headers=self.auth())
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(self.house.merchant.catalog[sku].price, (before * Decimal("1.12")).quantize(Decimal("0.01")))
        self.assertEqual(r.json()["field"], "price")
        r = self.client.post("/api/levers/delivery", json={"sku": sku, "delivery": "slow boat"}, headers=self.auth())
        self.assertEqual(self.house.merchant.catalog[sku].delivery, "slow boat")
        for path, body in (("/api/levers/price", {"sku": "nope", "price": "1"}),
                           ("/api/levers/price", {"sku": sku, "price": "1.001"}),
                           ("/api/levers/price", {"sku": sku, "price": "-3"}),
                           ("/api/levers/price", {"sku": sku, "price": "NaN"}),
                           ("/api/levers/stock", {"sku": sku, "stock": True}),
                           ("/api/levers/stock", {"sku": sku, "stock": -1}),
                           ("/api/levers/delivery", {"sku": sku, "delivery": ""}),
                           ("/api/levers/delivery", {"sku": sku, "delivery": "x" * 81})):
            r = self.client.post(path, json=body, headers=self.auth())
            self.assertEqual(r.status_code, 400, (path, body, r.text))
        self.assertEqual(self.client.post("/api/levers/stock", content=b"[1]", headers=self.auth()).status_code, 400)


@unittest.skipIf(TestClient is None, "needs the server extra")
class ConfigTest(HouseCase):
    def test_tokens_are_long_and_all_different(self):
        for env in ({"COUNTERSIGN_LEVER_TOKEN": "short"}, {"COUNTERSIGN_LEVER_TOKEN": MCP},
                    {"COUNTERSIGN_LEVER_TOKEN": SECRET + "x" * 0}, {"COUNTERSIGN_MCP_TOKEN": SECRET}):
            with self.assertRaises(ValueError, msg=env):
                house_from_env({**ENV, **env})

    def test_the_order_permalink_is_the_shoppers_page(self):
        s = self.session()
        self.sign(s["id"])
        done = self.acp(f"{SESSIONS}/{s['id']}/complete", {}).json()
        self.assertEqual(done["order"]["permalink_url"], f"{PUBLIC}/pay/{s['id']}")


class FakeCatalogClient:
    def __init__(self, fail=False):
        self.calls = []
        self.fail = fail

    def post(self, path, body):
        if self.fail:
            raise RuntimeError("Airwallex said no")
        self.calls.append((path, body))
        return {"records_updated": len(body["gmc_products"])}


@unittest.skipIf(TestClient is None, "needs the server extra")
class SyncTest(HouseCase):
    def test_a_lever_upserts_the_whole_product_to_airwallex(self):
        self.house.airwallex, self.house.sync_catalog = FakeCatalogClient(), True
        sku = self.house.merchant.search("kettle")[0].sku
        change = levers.set_stock(self.house, sku, 0)
        self.assertEqual(change.sync["ok"], True)
        (path, body), = self.house.airwallex.calls
        self.assertEqual(path, catalog_gmc.PRODUCTS_PATH)
        group = catalog_gmc.group_id(sku)
        variants = [s for s in self.house.merchant.catalog if catalog_gmc.group_id(s) == group]
        self.assertEqual(sorted(r["id"] for r in body["gmc_products"]), sorted(variants))  # all variants, one call
        row = next(r for r in body["gmc_products"] if r["id"] == sku)
        self.assertEqual(row["availability"], "out_of_stock")
        self.assertIn("Airwallex catalogue updated", self.house.feed.since(0)[-1]["detail"])

    def test_a_failed_sync_is_reported_and_the_change_stays(self):
        self.house.airwallex, self.house.sync_catalog = FakeCatalogClient(fail=True), True
        sku = self.house.merchant.search("kettle")[0].sku
        change = levers.set_delivery(self.house, sku, "slow boat")
        self.assertFalse(change.sync["ok"])
        self.assertIn("Airwallex said no", change.sync["error"])
        self.assertEqual(self.house.merchant.catalog[sku].delivery, "slow boat")

    def test_no_sync_unless_asked(self):
        sku = self.house.merchant.search("kettle")[0].sku
        self.assertIsNone(levers.set_stock(self.house, sku, 3).sync)


@unittest.skipIf(TestClient is None, "needs the server extra")
class PaymentFeedTest(HouseCase):
    def merchant(self):
        self.api = FakeAirwallex()
        return AirwallexMerchant(self.api, FakeFx("84.10"))

    def test_payments_settled_by_the_webhook_or_the_page_are_on_the_feed(self):
        for via in ("webhook", "page"):
            s = self.session()
            self.sign(s["id"])
            self.acp(f"{SESSIONS}/{s['id']}/complete", {})
            key = f"acp:{s['id']}"
            order = self.house.merchant.orders[key]
            decision = self.house.merchant._decisions[key][0]
            self.api.shopper_pays(order.payment_id, decision.charge, quote=decision.fx_quote_id)
            if via == "webhook":
                raw = json.dumps({"name": "payment_intent.succeeded", "data": {"object": {"id": order.payment_id}}}).encode()
                self.client.post(WEBHOOK_PATH, content=raw, headers=hmac_auth.sign(raw, b"w" * 32))
            else:
                self.client.post(f"/api/checkout/{s['id']}/confirm")
            last = self.house.feed.since(0)[-1]
            self.assertEqual((last["kind"], last["level"], last["order"]), ("payment", "go", order.order_number))
            self.assertIn("receipt signed", last["detail"])


if __name__ == "__main__":
    unittest.main()
