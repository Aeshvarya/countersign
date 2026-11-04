"""A7: one public host serves the demo, Airwallex's create_checkout, the shop window and the TAP door."""

import base64
import json
import unittest
import urllib.error
from datetime import datetime, timedelta, timezone

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey, Ed25519PublicKey

from countersign import receipt, tap
from countersign.agent import AgentKey, InProcessShop
from countersign.airwallex_merchant import AirwallexMerchant
from countersign.checkout import countersign
from countersign.fx import FakeFx
from countersign.merchant import FakeMerchant
from countersign.terms import Approval

try:
    from tests.test_airwallex_merchant import FakeAirwallex
except ImportError:  # run by discover with -s tests
    from test_airwallex_merchant import FakeAirwallex

try:
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from countersign.edge import hmac_auth
    from countersign.web.host import (KEY_PATH, WEBHOOK_PATH, Host, authority_of, house_from_env, merchant_from_env,
                                      signing_key_from)
except ImportError:  # the server extra is not installed
    TestClient = None

SECRET = "s" * 32
SEED = base64.b64encode(bytes(range(32))).decode()
PUBLIC = "https://countersign.onrender.com"
AGENT_DIR = "https://agents.example"
NOW = datetime(2026, 11, 2, 10, 0, tzinfo=timezone.utc)


def demo_app():
    demo = FastAPI()

    @demo.get("/")
    def index():
        return {"page": "demo"}

    return demo


@unittest.skipIf(TestClient is None, "needs the server extra: pip install -e '.[server]' httpx")
class HostTest(unittest.TestCase):
    def build(self, **env):
        environ = {"COUNTERSIGN_PUBLIC_URL": PUBLIC, "COUNTERSIGN_MERCHANT_SIGNING_KEY": SEED, **env}
        self.agent = Ed25519PrivateKey.generate()
        directory = tap.StaticDirectory({(AGENT_DIR, "k1"): self.agent.public_key()})
        self.house = house_from_env(environ, directory=directory)
        demo = demo_app()

        @demo.get(KEY_PATH)
        def key():
            return self.house.public_key()

        self.client = TestClient(Host(demo, self.house), base_url=PUBLIC)
        self.sku = next(iter(self.house.merchant.catalog))

    def acp(self, path, body, key):
        raw = json.dumps(body).encode()
        headers = {"content-type": "application/json", "Idempotency-Key": key,
                   **hmac_auth.sign(raw, SECRET.encode())}
        return self.client.post(path, content=raw, headers=headers)

    def test_the_demo_still_answers_everything_else(self):
        self.build()
        self.assertEqual(self.client.get("/").json(), {"page": "demo"})

    def test_the_shop_window_is_public(self):
        self.build()
        self.assertEqual(self.client.get(f"/p/{self.sku}").status_code, 200)
        r = self.client.get(f"/img/{self.sku}.svg")
        self.assertEqual((r.status_code, r.headers["content-type"]), (200, "image/svg+xml"))

    def test_create_checkout_end_to_end_with_a_receipt_anyone_can_verify(self):
        self.build(COUNTERSIGN_ACP_HMAC_SECRET=SECRET)
        self.assertTrue(self.house.acp)
        r = self.acp("/acp/checkout_sessions", {"items": [{"id": self.sku, "quantity": 1}], "currency": "INR"}, "c1")
        self.assertEqual(r.status_code, 201, r.text)
        session = r.json()
        self.assertEqual(session["status"], "ready_for_payment")
        done = self.acp(f"/acp/checkout_sessions/{session['id']}/complete", {}, "c2").json()
        self.assertEqual(done["status"], "completed", done)
        published = self.client.get(KEY_PATH).json()
        key = Ed25519PublicKey.from_public_bytes(base64.b64decode(published["public_key"]))
        self.assertEqual(published["key_id"], "merchant-key-1")
        self.assertTrue(receipt.verify(done["countersign"]["receipt"], key).ok)

    def test_an_unsigned_create_checkout_is_refused(self):
        self.build(COUNTERSIGN_ACP_HMAC_SECRET=SECRET)
        r = self.client.post("/acp/checkout_sessions", json={"items": []}, headers={"Idempotency-Key": "x"})
        self.assertEqual(r.status_code, 401)

    def test_without_a_secret_acp_stays_behind_tap(self):
        self.build()
        self.assertFalse(self.house.acp)
        self.assertEqual(self.client.post("/acp/checkout_sessions", json={}).status_code, 401)

    def test_the_tap_door_checks_against_the_public_host(self):
        self.build()
        self.assertEqual(self.client.get("/products?q=kettle").status_code, 401)
        shop = InProcessShop(self.client.app, AgentKey(self.agent, "k1", AGENT_DIR), authority_of(PUBLIC))
        self.assertTrue(shop.search("kettle", 3))

    def test_bad_configuration_stops_the_service(self):
        with self.assertRaises(ValueError):
            authority_of("http://plain.example")
        self.assertEqual(authority_of("http://localhost:8765"), "localhost:8765")  # passkeys work there
        with self.assertRaises(ValueError):
            signing_key_from({"COUNTERSIGN_MERCHANT_SIGNING_KEY": base64.b64encode(b"short").decode()})
        with self.assertRaises(ValueError):
            house_from_env({"COUNTERSIGN_ACP_HMAC_SECRET": "too short"})

    def test_the_merchant_switch(self):
        self.assertIsInstance(merchant_from_env({}), FakeMerchant)
        with self.assertRaises(ValueError):
            merchant_from_env({"COUNTERSIGN_MERCHANT": "airwallex"})  # no keys
        with self.assertRaises(ValueError):
            merchant_from_env({"COUNTERSIGN_MERCHANT": "stripe"})
        keys = {"COUNTERSIGN_MERCHANT": "airwallex", "AIRWALLEX_CLIENT_ID": "id", "AIRWALLEX_API_KEY": "k"}
        self.assertIsInstance(merchant_from_env(keys), AirwallexMerchant)  # builds without calling anyone

    def test_the_signing_key_survives_a_restart(self):
        a = signing_key_from({"COUNTERSIGN_MERCHANT_SIGNING_KEY": SEED}).public_key()
        b = signing_key_from({"COUNTERSIGN_MERCHANT_SIGNING_KEY": SEED}).public_key()
        self.assertEqual(a.public_bytes_raw(), b.public_bytes_raw())


WEBHOOK_SECRET = "w" * 32


@unittest.skipIf(TestClient is None, "needs the server extra: pip install -e '.[server]' httpx")
class WebhookTest(unittest.TestCase):
    """C9 on the host: Airwallex's event settles the payment and releases the held receipt."""

    def setUp(self):
        self.api = FakeAirwallex()
        merchant = AirwallexMerchant(self.api, FakeFx("84.10"))
        environ = {"COUNTERSIGN_MERCHANT_SIGNING_KEY": SEED, "AIRWALLEX_WEBHOOK_SECRET": WEBHOOK_SECRET}
        self.house = house_from_env(environ, merchant=merchant)
        self.client = TestClient(Host(demo_app(), self.house))
        sku = merchant.search("kettle")[0].sku
        terms = merchant.offer({sku: 1}, "INR", NOW)
        self.approval = Approval.grant(terms, terms.shopper_total, "s", NOW, NOW + timedelta(hours=2))
        # The same store the edge holds receipts in, as a checkout through either door would.
        self.result = countersign(merchant, self.approval, "k1", NOW, signer=self.house.signer, store=self.house.store)

    def event(self, intent_id, *, secret=WEBHOOK_SECRET, name="payment_intent.succeeded"):
        raw = json.dumps({"name": name, "data": {"object": {"id": intent_id}}}).encode()
        headers = {"content-type": "application/json", **hmac_auth.sign(raw, secret.encode())}
        return self.client.post(WEBHOOK_PATH, content=raw, headers=headers)

    def test_a_paid_intent_releases_a_receipt_anyone_can_verify(self):
        r = self.result
        self.assertIsNone(self.house.store.get("k1"))
        self.api.shopper_pays(r.order.payment_id, r.decision.charge, quote=r.decision.fx_quote_id)
        out = self.event(r.order.payment_id).json()
        self.assertEqual((out["status"], out["receipt"]), ("paid", True))
        signed = self.house.store.get("k1")
        public = self.house.signer.key.public_key()
        self.assertTrue(receipt.verify(signed, public, self.approval).ok)
        self.assertEqual(self.event(r.order.payment_id).json()["status"], "paid")  # Airwallex retries

    def test_an_event_before_the_payment_succeeds_does_not_lose_the_receipt(self):
        # Subscribed to payment_intent.*, the first event is payment_intent.created, while still pending.
        r = self.result
        early = self.event(r.order.payment_id, name="payment_intent.created").json()
        self.assertEqual((early["status"], early["receipt"]), ("pending", False))
        self.api.shopper_pays(r.order.payment_id, r.decision.charge, quote=r.decision.fx_quote_id)
        out = self.event(r.order.payment_id).json()
        self.assertEqual((out["status"], out["receipt"]), ("paid", True))
        self.assertTrue(receipt.verify(self.house.store.get("k1"), self.house.signer.key.public_key(),
                                       self.approval).ok)

    def test_an_event_while_airwallex_does_not_answer_asks_for_a_retry(self):
        def timed_out(path, params=None):
            raise urllib.error.URLError("connection reset")

        self.api.get = timed_out
        r = self.event(self.result.order.payment_id)
        self.assertEqual(r.status_code, 503)  # Airwallex retries the event; nothing is settled yet
        self.assertEqual(self.house.merchant.orders["k1"].status, "pending")
        self.assertIsNone(self.house.store.get("k1"))

    def test_a_forged_event_is_refused(self):
        r = self.event(self.result.order.payment_id, secret="x" * 32)
        self.assertEqual(r.status_code, 401)
        self.assertEqual(self.house.merchant.orders["k1"].status, "pending")

    def test_events_about_other_things_are_acknowledged_and_ignored(self):
        self.assertTrue(self.event("int_not_ours").json()["ignored"])
        self.assertTrue(self.event("rfd_1", name="refund.succeeded").json()["ignored"])

    def test_without_a_secret_every_event_is_refused(self):
        house = house_from_env({}, merchant=AirwallexMerchant(self.api, FakeFx("84.10")))
        client = TestClient(Host(demo_app(), house))
        raw = b'{"name": "payment_intent.succeeded"}'
        self.assertEqual(client.post(WEBHOOK_PATH, content=raw, headers=hmac_auth.sign(raw, b"")).status_code, 401)


@unittest.skipIf(TestClient is None, "needs the server extra: pip install -e '.[server]' httpx")
class KeepAwakeTest(unittest.TestCase):
    def test_it_pings_our_own_health_check_until_stopped(self):
        import threading

        from countersign.web.host import keep_awake

        seen, stop = [], threading.Event()

        def fetch(url):
            seen.append(url)
            if len(seen) == 3:
                stop.set()
            return 200

        keep_awake("https://shop.example/", every=0.01, fetch=fetch, stop=stop).join(2)
        self.assertEqual(seen, ["https://shop.example/healthz"] * 3)

    def test_a_failed_ping_is_logged_and_the_next_one_still_goes(self):
        import threading

        from countersign.web.host import keep_awake

        calls, stop = [], threading.Event()

        def fetch(url):
            calls.append(url)
            if len(calls) == 2:
                stop.set()
            raise OSError("host asleep")

        with self.assertLogs("countersign.host", "WARNING"):
            keep_awake("https://shop.example", every=0.01, fetch=fetch, stop=stop).join(2)
        self.assertEqual(len(calls), 2)

    def test_it_is_off_unless_asked_and_needs_a_public_url(self):
        self.assertFalse(house_from_env({}).keep_awake)
        self.assertTrue(house_from_env({"COUNTERSIGN_PUBLIC_URL": PUBLIC, "COUNTERSIGN_KEEP_AWAKE": "1"}).keep_awake)
        with self.assertRaises(ValueError):
            house_from_env({"COUNTERSIGN_KEEP_AWAKE": "1"})

    def test_the_health_check_says_whether_it_and_the_oauth_server_are_on(self):
        from countersign.web.public import router

        app = FastAPI()
        app.include_router(router(house_from_env({"COUNTERSIGN_PUBLIC_URL": PUBLIC, "COUNTERSIGN_KEEP_AWAKE": "1"})))
        health = TestClient(app).get("/healthz").json()
        self.assertEqual((health["oauth"], health["keep_awake"]), (True, True))
        app = FastAPI()
        app.include_router(router(house_from_env({})))
        health = TestClient(app).get("/healthz").json()
        self.assertEqual((health["oauth"], health["keep_awake"]), (False, False))


if __name__ == "__main__":
    unittest.main()
