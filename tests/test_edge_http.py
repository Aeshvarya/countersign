"""V2 done-when: HttpShop pointed at a running edge passes the flows LocalShop does in test_agent."""

import socket
import threading
import time
import unittest
import urllib.request
from datetime import datetime, timezone
from decimal import Decimal

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from countersign import receipt, tap
from countersign.agent import AgentKey, AgentSession, HttpShop, ScriptedModel
from countersign.fx import FakeFx
from countersign.merchant import FakeMerchant

try:
    import uvicorn

    from countersign.edge import create_app
except ImportError:  # the server extra is not installed
    uvicorn = None

AGENT = "https://agents.example"


class Clock:
    def __init__(self):
        self.now = datetime(2026, 11, 2, 10, 0, tzinfo=timezone.utc)

    def __call__(self):
        return self.now


class LiveEdge:
    """The edge on a real socket, so HttpShop's own HTTP client is what gets tested."""

    def __init__(self, merchant, directory, clock, signer):
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.sock.bind(("127.0.0.1", 0))
        self.authority = f"127.0.0.1:{self.sock.getsockname()[1]}"
        self.app = create_app(merchant, directory, authority=self.authority, clock=clock, signer=signer)
        self.server = uvicorn.Server(uvicorn.Config(self.app, log_level="warning"))
        self.thread = threading.Thread(target=self.server.run, kwargs={"sockets": [self.sock]}, daemon=True)

    def start(self):
        self.thread.start()
        deadline = time.time() + 5
        while not self.server.started:
            if time.time() > deadline:
                raise RuntimeError("the edge did not start")
            time.sleep(0.01)
        return self

    def stop(self):
        self.server.should_exit = True
        self.thread.join(5)
        self.sock.close()


@unittest.skipIf(uvicorn is None, "needs the server extra: pip install -e '.[server]' httpx")
class HttpShopAgainstTheEdgeTest(unittest.TestCase):
    def build(self, known_agent=True):
        self.clock = Clock()
        self.fx = FakeFx("84.10")
        self.merchant = FakeMerchant(self.fx)
        agent_key = AgentKey(Ed25519PrivateKey.generate(), "agent-key-1", AGENT)
        keys = {(AGENT, "agent-key-1"): agent_key.key.public_key()} if known_agent else {}
        self.merchant_key = Ed25519PrivateKey.generate()
        self.edge = LiveEdge(self.merchant, tap.StaticDirectory(keys), self.clock,
                             receipt.MerchantSigner(self.merchant_key, "m-1")).start()
        self.addCleanup(self.edge.stop)
        shop = HttpShop(f"http://{self.edge.authority}", agent_key, currency=self.merchant.currency)
        return AgentSession(shop, ScriptedModel(), "shopper@example.com", "INR", clock=self.clock)

    def test_happy_path_pauses_for_approval_then_pays_with_a_receipt(self):
        session = self.build()
        session.start("buy me a mechanical keyboard")
        self.assertEqual(session.status, "awaiting_approval")
        self.assertEqual(self.merchant.orders, {})

        session.approve(session.suggested_ceiling())
        self.assertEqual(session.status, "done")
        self.assertEqual(len(self.merchant.orders), 1)
        self.assertEqual(len(session.receipts), 1)
        self.assertTrue(receipt.verify(session.receipts[0], self.merchant_key.public_key()).ok)
        log = self.edge.app.state.edge_log
        self.assertTrue(all(e["ok"] for e in log))
        self.assertEqual({e["tag"] for e in log}, {tap.BROWSE, tap.PAY})

    def test_rate_moves_past_the_ceiling_so_the_agent_asks_again_then_pays(self):
        session = self.build()
        session.start("buy me a mechanical keyboard")
        self.fx.rate = Decimal("88.40")
        session.approve()
        self.assertEqual(session.status, "awaiting_approval")
        decision = next(e for e in session.events if e["kind"] == "decision")
        self.assertEqual(decision["data"]["decision"]["outcome"], "reask")
        self.assertEqual(self.merchant.orders, {})
        session.approve(session.suggested_ceiling())
        self.assertEqual(session.status, "done")
        self.assertEqual(len(self.merchant.orders), 1)

    def test_decline_charges_nothing(self):
        session = self.build()
        session.start("buy me a kettle")
        session.decline()
        self.assertEqual(session.status, "done")
        self.assertEqual(self.merchant.orders, {})

    def test_an_agent_whose_key_the_merchant_does_not_know_is_refused_at_the_edge(self):
        session = self.build(known_agent=False)
        session.start("buy me a kettle")
        self.assertEqual(self.merchant.orders, {})
        self.assertFalse(self.edge.app.state.edge_log[0]["ok"])
        self.assertTrue(any(e["kind"] == "edge" for e in session.events))



class SlowDirectory:
    """One agent's key lookup takes a second, like a registry having a bad day."""

    def __init__(self, keys):
        self.keys = keys

    def public_key(self, signature_agent, key_id):
        if key_id == "slow-key":
            time.sleep(1)
        return self.keys.get((signature_agent, key_id))


@unittest.skipIf(uvicorn is None, "needs the server extra: pip install -e '.[server]' httpx")
class SlowDirectoryTest(unittest.TestCase):
    def test_a_slow_key_lookup_does_not_hold_up_other_agents(self):
        fast, slow = Ed25519PrivateKey.generate(), Ed25519PrivateKey.generate()
        directory = SlowDirectory({(AGENT, "fast-key"): fast.public_key(), (AGENT, "slow-key"): slow.public_key()})
        edge = LiveEdge(FakeMerchant(FakeFx("84.10")), directory, Clock(), None).start()
        self.addCleanup(edge.stop)

        def get(key, key_id):
            path = "/products?q=kettle"
            headers = tap.sign(key, authority=edge.authority, path=path, key_id=key_id,
                               signature_agent=AGENT, tag=tap.BROWSE).headers()
            with urllib.request.urlopen(urllib.request.Request(f"http://{edge.authority}{path}", headers=headers)) as r:
                return r.status

        slow_call = threading.Thread(target=get, args=(slow, "slow-key"))
        slow_call.start()
        time.sleep(0.1)
        started = time.monotonic()
        self.assertEqual(get(fast, "fast-key"), 200)
        self.assertLess(time.monotonic() - started, 0.5)
        slow_call.join()


if __name__ == "__main__":
    unittest.main()
