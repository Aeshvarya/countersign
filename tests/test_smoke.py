"""scripts/smoke.py against the real host app, in process: every check it makes passes on a good deploy."""

import base64
import hashlib
import importlib.util
import json
import pathlib
import sys
import unittest

from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec

_PATH = pathlib.Path(__file__).resolve().parent.parent / "scripts" / "smoke.py"
_spec = importlib.util.spec_from_file_location("smoke", _PATH)
smoke = importlib.util.module_from_spec(_spec)
sys.modules["smoke"] = smoke  # dataclasses look the module up
_spec.loader.exec_module(smoke)

try:
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from countersign.edge.passkey import b64url
    from countersign.web import public
    from countersign.web.host import Host, house_from_env
except ImportError:  # the server extra is not installed
    TestClient = None

PUBLIC = "https://countersign-demo.onrender.com"
SECRET, MCP, LEVER = "s" * 32, "m" * 40, "L" * 40
ENV = {"COUNTERSIGN_PUBLIC_URL": PUBLIC, "COUNTERSIGN_ACP_HMAC_SECRET": SECRET, "COUNTERSIGN_MCP_TOKEN": MCP,
       "COUNTERSIGN_LEVER_TOKEN": LEVER, "COUNTERSIGN_MERCHANT_SIGNING_KEY": base64.b64encode(bytes(range(32))).decode()}


@unittest.skipIf(TestClient is None, "needs the server extra")
class SmokeTest(unittest.TestCase):
    def client(self, **env):
        self.house = house_from_env({**ENV, **env})
        app = FastAPI()
        app.include_router(public.router(self.house))  # what web.app serves for its house
        return TestClient(Host(app, self.house), base_url=PUBLIC)

    def run_smoke(self, client, *, buy=False, sleep=lambda s: None, **secrets):
        lines = []
        s = smoke.Smoke(client, PUBLIC, out=lines.append, sleep=sleep, **secrets)
        return s.run(buy=buy), s, "\n".join(lines)

    def test_a_good_deploy_passes_everything_it_can_see(self):
        ok, s, text = self.run_smoke(self.client(), secret=SECRET, mcp_token=MCP, lever_token=LEVER)
        self.assertTrue(ok, text)
        self.assertFalse([c for c in s.checks if c.ok is None], text)
        for secret in (SECRET, MCP, LEVER):
            self.assertNotIn(secret, text)

    def test_without_secrets_it_checks_the_public_doors_and_skips_the_rest(self):
        ok, s, text = self.run_smoke(self.client())
        self.assertTrue(ok, text)
        self.assertEqual({c.name for c in s.checks if c.ok is None},
                         {"checkout as Airwallex calls it", "fallback MCP", "merchant console"})

    def test_a_wrong_secret_or_url_fails(self):
        ok, _, text = self.run_smoke(self.client(), secret="x" * 32)
        self.assertFalse(ok)
        self.assertIn("the secret here is not the one on the host", text)
        ok, _, text = self.run_smoke(self.client(COUNTERSIGN_PUBLIC_URL="https://elsewhere.example"))
        self.assertFalse(ok)
        self.assertIn("FAIL  TAP authority is this host", text)

    def test_buy_waits_for_the_shopper_then_checks_the_receipt(self):
        client = self.client(COUNTERSIGN_REQUIRE_SHOPPER_SIGNATURE="1")
        device = ec.generate_private_key(ec.SECP256R1())

        def shopper_signs(_):  # while the script waits, the shopper approves on their page
            api = self.house.edge.state.acp
            for sid, sess in api.sessions.items():
                if sess.status == "canceled" or not api.awaiting_shopper(sess):
                    continue
                data = client.get(f"/acp/approvals/{sid}").json()
                ad = hashlib.sha256(b"countersign-demo.onrender.com").digest() + b"\x05" + (1).to_bytes(4, "big")
                cdj = json.dumps({"type": "webauthn.get", "challenge": data["webauthn"]["challenge"],
                                  "origin": PUBLIC}).encode()
                sig = device.sign(ad + hashlib.sha256(cdj).digest(), ec.ECDSA(hashes.SHA256()))
                spki = device.public_key().public_bytes(serialization.Encoding.DER,
                                                        serialization.PublicFormat.SubjectPublicKeyInfo)
                client.post(f"/acp/approvals/{sid}", json={
                    "credential_id": b64url(b"d"), "public_key_spki_der": b64url(spki),
                    "authenticator_data": b64url(ad), "client_data_json": b64url(cdj), "signature": b64url(sig)})

        ok, s, text = self.run_smoke(client, buy=True, sleep=shopper_signs, secret=SECRET)
        self.assertTrue(ok, text)
        self.assertIn("PASS  receipt verifies against the published key", text)
        self.assertIn("waiting for the shopper's passkey", text)


if __name__ == "__main__":
    unittest.main()
