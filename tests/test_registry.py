"""V3: reading agent keys from a registry with the API of Visa's reference agent-registry."""

import base64
import json
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from countersign import tap

AGENT_1 = "https://tapagent.com"   # the two agents in Visa's sample data,
AGENT_2 = "https://tapagent2.com"  # both with a key called "primary"
NOW = 1_793_000_000


def b64(key):
    return base64.b64encode(key.public_key().public_bytes(serialization.Encoding.Raw,
                                                          serialization.PublicFormat.Raw)).decode()


def same_key(a, b):
    raw = lambda k: k.public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)
    return raw(a) == raw(b)


class FakeRegistry:
    """Answers like Visa's agent-registry; records every path asked for."""

    def __init__(self, routes):
        self.routes = routes
        self.requests = []
        outer = self

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):
                outer.requests.append(self.path)
                status, body = outer.routes.get(self.path, (404, {"detail": "Not Found"}))
                data = json.dumps(body).encode()
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def log_message(self, *args):
                pass

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}"
        threading.Thread(target=self.server.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True).start()

    def stop(self):
        self.server.shutdown()
        self.server.server_close()


def key_entry(key_id, public, algorithm="ed25519", active="true"):
    return {"id": 1, "agent_id": 1, "key_id": key_id, "public_key": public, "algorithm": algorithm,
            "description": None, "is_active": active, "created_at": "2026-10-13T00:00:00", "updated_at": "2026-10-13T00:00:00"}


class Clock:
    def __init__(self):
        self.t = 1000.0

    def __call__(self):
        return self.t


class RegistryDirectoryTest(unittest.TestCase):
    def setUp(self):
        self.k1, self.k2, self.old = (Ed25519PrivateKey.generate() for _ in range(3))
        rsa_pem = "-----BEGIN PUBLIC KEY-----\nMIIB\n-----END PUBLIC KEY-----"
        self.agents = [
            {"id": 1, "name": "Sample Agent", "domain": AGENT_1, "description": None, "is_active": "true",
             "keys": [key_entry("primary", b64(self.k1)), key_entry("backup-2024", rsa_pem, "RSA-SHA256"),
                      key_entry("retired", b64(self.old), active="false")]},
            {"id": 2, "name": "Second Agent", "domain": AGENT_2, "description": None, "is_active": "true",
             "keys": [key_entry("primary", b64(self.k2))]},
        ]
        self.registry = FakeRegistry({"/agents": (200, self.agents)})
        self.addCleanup(self.registry.stop)
        self.clock = Clock()
        self.directory = tap.RegistryDirectory(self.registry.url, ttl=60, clock=self.clock)

    def test_the_same_key_id_resolves_to_each_agents_own_key(self):
        self.assertTrue(same_key(self.directory.public_key(AGENT_1, "primary"), self.k1.public_key()))
        self.assertTrue(same_key(self.directory.public_key(AGENT_2, "primary"), self.k2.public_key()))

    def test_only_active_ed25519_keys_of_listed_agents_count(self):
        for agent, key_id in [(AGENT_1, "retired"), (AGENT_1, "backup-2024"), (AGENT_1, "nope"),
                              ("https://stranger.example", "primary")]:
            with self.subTest(agent=agent, key_id=key_id):
                self.assertIsNone(self.directory.public_key(agent, key_id))

    def test_a_malformed_key_id_never_reaches_the_registry(self):
        self.assertIsNone(self.directory.public_key(AGENT_1, "../agents"))
        self.assertEqual(self.registry.requests, [])

    def test_answers_are_cached_and_a_revoked_key_stops_working_after_the_ttl(self):
        self.assertIsNotNone(self.directory.public_key(AGENT_1, "primary"))
        self.assertIsNotNone(self.directory.public_key(AGENT_2, "primary"))
        self.assertIsNone(self.directory.public_key(AGENT_1, "unknown-1"))
        self.assertEqual(self.registry.requests, ["/agents"])
        self.agents[0]["keys"][0]["is_active"] = "false"
        self.clock.t += 61
        self.assertIsNone(self.directory.public_key(AGENT_1, "primary"))
        self.assertEqual(self.registry.requests, ["/agents", "/agents"])

    def test_a_signed_request_verifies_through_the_registry(self):
        signed = tap.sign(self.k1, authority="shop.example", path="/checkout", key_id="primary",
                          signature_agent=AGENT_1, tag=tap.PAY, now=NOW)
        args = dict(authority="shop.example", path="/checkout", directory=self.directory, now=NOW + 5)
        self.assertIsInstance(tap.verify(signed.headers(), nonces=tap.MemoryNonceCache(), **args), tap.AgentIdentity)
        as_other = tap.sign(self.k1, authority="shop.example", path="/checkout", key_id="primary",
                            signature_agent=AGENT_2, tag=tap.PAY, now=NOW)
        result = tap.verify(as_other.headers(), nonces=tap.MemoryNonceCache(), **args)
        self.assertIn("does not match", result.reason)  # agent 2's "primary" is a different key


class FallbackTest(unittest.TestCase):
    """A registry with no agent list: GET /keys/{key_id}, bound to agent_domain."""

    def setUp(self):
        self.k1 = Ed25519PrivateKey.generate()
        entry = {"key_id": "primary", "is_active": "true", "public_key": b64(self.k1), "algorithm": "ed25519",
                 "description": None, "agent_id": 1, "agent_name": "Sample Agent", "agent_domain": AGENT_1}
        self.registry = FakeRegistry({"/keys/primary": (200, entry),
                                      "/keys/no-domain": (200, {**entry, "key_id": "no-domain", "agent_domain": None})})
        self.addCleanup(self.registry.stop)
        self.directory = tap.RegistryDirectory(self.registry.url)

    def test_a_key_counts_only_for_the_agent_the_registry_names(self):
        self.assertTrue(same_key(self.directory.public_key(AGENT_1, "primary"), self.k1.public_key()))
        self.assertIsNone(self.directory.public_key(AGENT_2, "primary"))
        self.assertIsNone(self.directory.public_key(AGENT_1, "no-domain"))
        self.assertIsNone(self.directory.public_key(AGENT_1, "missing"))


class UnavailableTest(unittest.TestCase):
    def test_a_registry_that_is_down_or_broken_is_unavailable_not_empty(self):
        down = FakeRegistry({})
        down.stop()
        broken = FakeRegistry({"/agents": (500, {"detail": "db locked"})})
        self.addCleanup(broken.stop)
        for name, url in [("down", down.url), ("500", broken.url)]:
            with self.subTest(name):
                with self.assertRaises(tap.DirectoryUnavailable):
                    tap.RegistryDirectory(url, timeout=1).public_key(AGENT_1, "primary")


if __name__ == "__main__":
    unittest.main()
