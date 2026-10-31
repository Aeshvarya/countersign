"""U1: the shopper's approval page at /approve/<id>, served by the one public host next to the routes it calls."""

import base64
import hashlib
import json
import re
import unittest
from pathlib import Path
from urllib.parse import urlsplit

from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec

from countersign import terms

try:
    from fastapi.testclient import TestClient

    from countersign.edge import hmac_auth
    from countersign.edge.passkey import b64url
    from countersign.web.app import demo
    from countersign.web.host import Host, house_from_env
except ImportError:  # the server extra is not installed
    TestClient = None

STATIC = Path(__file__).resolve().parent.parent / "src" / "countersign" / "web" / "static"
PUBLIC = "https://countersign.onrender.com"
SECRET = "s" * 32
SESSIONS = "/acp/checkout_sessions"


class ScriptTest(unittest.TestCase):
    def test_the_page_formats_amounts_with_the_servers_minor_units(self):
        # The approval routes send integer minor units; the page must divide by the same power of ten.
        source = (STATIC / "approve.js").read_text()
        table = re.search(r"const MINOR_UNITS = (\{.*?\});", source)
        self.assertIsNotNone(table, "approve.js lost its MINOR_UNITS table")
        self.assertEqual(json.loads(table.group(1)), terms._MINOR_UNITS)


@unittest.skipIf(TestClient is None, "needs the server extra: pip install -e '.[server]' httpx")
class ApprovalPageTest(unittest.TestCase):
    def setUp(self):
        self.house = house_from_env({
            "COUNTERSIGN_PUBLIC_URL": PUBLIC,
            "COUNTERSIGN_MERCHANT_SIGNING_KEY": base64.b64encode(bytes(range(32))).decode(),
            "COUNTERSIGN_ACP_HMAC_SECRET": SECRET,
            "COUNTERSIGN_REQUIRE_SHOPPER_SIGNATURE": "1",
        })
        self.client = TestClient(Host(demo, self.house), base_url=PUBLIC)
        self.keys = 0

    def post(self, path, body):
        self.keys += 1
        raw = json.dumps(body).encode()
        r = self.client.post(path, content=raw, headers={**hmac_auth.sign(raw, SECRET.encode()),
                                                          "content-type": "application/json",
                                                          "Idempotency-Key": f"page-{self.keys}"})
        self.assertLess(r.status_code, 300, r.text)
        return r.json()

    def session(self):
        sku = next(iter(self.house.merchant.catalog))
        return self.post(SESSIONS, {"items": [{"id": sku, "quantity": 1}], "currency": "INR"})

    def test_the_link_in_the_session_opens_the_page(self):
        s = self.session()
        link = next(link["url"] for link in s["links"] if link["type"] == "approval")
        self.assertTrue(link.startswith(PUBLIC + "/approve/"))
        page = self.client.get(urlsplit(link).path)
        self.assertEqual(page.status_code, 200)
        self.assertIn("text/html", page.headers["content-type"])
        self.assertIn('<script src="/static/approve.js"></script>', page.text)
        script = self.client.get("/static/approve.js")
        self.assertEqual(script.status_code, 200)
        self.assertIn("javascript", script.headers["content-type"])

    def test_the_page_cannot_be_framed_or_leak_its_address(self):
        page = self.client.get("/approve/cs_anything")
        csp = page.headers["content-security-policy"]
        self.assertIn("frame-ancestors 'none'", csp)
        self.assertIn("default-src 'none'", csp)
        self.assertIn("connect-src 'self'", csp)
        self.assertEqual(page.headers["referrer-policy"], "no-referrer")
        self.assertEqual(page.headers["x-frame-options"], "DENY")
        self.assertEqual(page.headers["cache-control"], "no-store")
        # Nothing on the page comes from another site.
        self.assertNotRegex(page.text, r"(src|href)=\"(https?:)?//")

    def test_what_the_page_does_is_accepted_and_complete_goes_through(self):
        s = self.session()
        data = self.client.get(f"/acp/approvals/{s['id']}").json()
        self.assertEqual((data["webauthn"]["origin"], data["webauthn"]["rp_id"]),
                         (PUBLIC, "countersign.onrender.com"))
        held = self.post(f"{SESSIONS}/{s['id']}/complete", {})
        self.assertEqual(held["messages"][0]["code"], "approval_required")

        # A device signs the server's challenge bytes as navigator.credentials.get() does.
        device = ec.generate_private_key(ec.SECP256R1())
        authenticator_data = hashlib.sha256(b"countersign.onrender.com").digest() + b"\x05" + (1).to_bytes(4, "big")
        client_data_json = json.dumps({"type": "webauthn.get", "challenge": data["webauthn"]["challenge"],
                                       "origin": data["webauthn"]["origin"], "crossOrigin": False}).encode()
        signature = device.sign(authenticator_data + hashlib.sha256(client_data_json).digest(),
                                ec.ECDSA(hashes.SHA256()))
        spki = device.public_key().public_bytes(serialization.Encoding.DER,
                                                serialization.PublicFormat.SubjectPublicKeyInfo)
        r = self.client.post(f"/acp/approvals/{s['id']}", json={
            "credential_id": b64url(b"device-1"), "public_key_spki_der": b64url(spki),
            "authenticator_data": b64url(authenticator_data), "client_data_json": b64url(client_data_json),
            "signature": b64url(signature)})
        self.assertEqual(r.status_code, 200, r.text)
        self.assertTrue(self.client.get(f"/acp/approvals/{s['id']}").json()["signed"])

        done = self.post(f"{SESSIONS}/{s['id']}/complete", {})
        self.assertEqual(done["status"], "completed")


if __name__ == "__main__":
    unittest.main()
