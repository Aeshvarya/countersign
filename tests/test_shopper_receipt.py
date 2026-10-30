"""C13: the shopper's passkey signature travels in the receipt and is checked offline; old receipts still verify."""

import copy
import hashlib
import json
import unittest
from dataclasses import replace
from datetime import datetime, timedelta, timezone

from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from countersign import receipt, tap, wire
from countersign.edge.passkey import b64url
from countersign.fx import FakeFx
from countersign.merchant import FakeMerchant
from countersign.terms import Approval

try:
    from fastapi.testclient import TestClient

    from countersign.edge import create_app, hmac_auth
    from countersign.edge.acp import AcpConfig
except ImportError:  # the server extra is not installed
    TestClient = None

SECRET = b"k" * 32
SESSIONS = "/acp/checkout_sessions"
NOW = datetime(2026, 11, 2, 10, 0, tzinfo=timezone.utc)


@unittest.skipIf(TestClient is None, "needs the server extra: pip install -e '.[server]' httpx")
class ShopperReceiptTest(unittest.TestCase):
    def setUp(self):
        self.merchant = FakeMerchant(FakeFx("84.10"))
        self.merchant_key = Ed25519PrivateKey.generate()
        self.signer = receipt.MerchantSigner(self.merchant_key, "m-1")
        self.sku = next(iter(self.merchant.catalog))
        self.device = ec.generate_private_key(ec.SECP256R1())
        self.keys = 0

    def client(self, *, u1=True):
        app = create_app(self.merchant, tap.StaticDirectory({}), authority="testserver", clock=lambda: NOW,
                         signer=self.signer, acp=AcpConfig(secret=SECRET, require_shopper_signature=u1,
                                                           public_url="https://shop.example"))
        return TestClient(app)

    def post(self, client, path, body):
        self.keys += 1
        raw = json.dumps(body).encode()
        return client.post(path, content=raw, headers={**hmac_auth.sign(raw, SECRET), "content-type": "application/json",
                                                       "Idempotency-Key": f"r-{self.keys}"}).json()

    def shopper_signs(self, client, sid):
        challenge = client.get(f"/acp/approvals/{sid}").json()["webauthn"]["challenge"]
        authenticator_data = hashlib.sha256(b"shop.example").digest() + b"\x05" + (3).to_bytes(4, "big")
        client_data_json = json.dumps({"type": "webauthn.get", "challenge": challenge,
                                       "origin": "https://shop.example"}).encode()
        signature = self.device.sign(authenticator_data + hashlib.sha256(client_data_json).digest(),
                                     ec.ECDSA(hashes.SHA256()))
        public_key = self.device.public_key().public_bytes(serialization.Encoding.DER,
                                                           serialization.PublicFormat.SubjectPublicKeyInfo)
        r = client.post(f"/acp/approvals/{sid}", json={
            "credential_id": b64url(b"credential-1"), "public_key_spki_der": b64url(public_key),
            "authenticator_data": b64url(authenticator_data), "client_data_json": b64url(client_data_json),
            "signature": b64url(signature)})
        self.assertEqual(r.status_code, 200, r.text)

    def signed_receipt(self):
        client = self.client()
        s = self.post(client, SESSIONS, {"items": [{"id": self.sku, "quantity": 1}], "currency": "INR"})
        self.shopper_signs(client, s["id"])
        done = self.post(client, f"{SESSIONS}/{s['id']}/complete", {})
        self.assertEqual(done["status"], "completed")
        return s["id"], done["countersign"]["receipt"]

    def test_the_receipt_carries_the_shoppers_signature_and_it_checks_offline(self):
        sid, signed = self.signed_receipt()
        recorded = signed["body"]["approval"]["shopper_assertion"]
        self.assertEqual((recorded["session_id"], recorded["origin"], recorded["rp_id"]),
                         (sid, "https://shop.example", "shop.example"))
        offline = json.loads(json.dumps(signed))  # as read back from a file, with no server
        check = receipt.verify(offline, self.merchant_key.public_key())
        self.assertTrue(check.ok, check.findings)
        self.assertIn("the shopper's own passkey signed these exact terms at https://shop.example (user verified)",
                      check.findings)

    def test_a_bad_shopper_signature_fails_even_under_a_valid_merchant_signature(self):
        _, signed = self.signed_receipt()
        for change, reason in (
            (lambda r: r.update(session_id="cs_other"), "the challenge is not for these terms"),
            (lambda r: r.update(origin="https://evil.example"), "the origin does not match"),
            (lambda r: r.pop("signature"), "the recorded signature is incomplete"),
        ):
            body = copy.deepcopy(signed["body"])
            change(body["approval"]["shopper_assertion"])
            check = receipt.verify(receipt.sign(body, self.signer), self.merchant_key.public_key())
            self.assertFalse(check.ok)
            self.assertEqual(check.findings[-1], f"the shopper's passkey signature does not check: {reason}")

    def test_a_signature_for_other_terms_fails(self):
        _, signed = self.signed_receipt()
        body = copy.deepcopy(signed["body"])
        body["approval"]["hash"] = "00" * 32
        check = receipt.verify(receipt.sign(body, self.signer), self.merchant_key.public_key())
        self.assertEqual(check.findings[-1],
                         "the shopper's passkey signature does not check: the challenge is not for these terms")

    def test_editing_the_recorded_signature_breaks_the_merchants_signature(self):
        _, signed = self.signed_receipt()
        edited = copy.deepcopy(signed)
        edited["body"]["approval"]["shopper_assertion"]["origin"] = "https://evil.example"
        check = receipt.verify(edited, self.merchant_key.public_key())
        self.assertEqual(check.findings, ("the merchant's signature does not match this receipt",))

    def test_a_receipt_without_u1_is_unchanged_and_still_verifies(self):
        client = self.client(u1=False)
        s = self.post(client, SESSIONS, {"items": [{"id": self.sku, "quantity": 1}], "currency": "INR"})
        done = self.post(client, f"{SESSIONS}/{s['id']}/complete", {})
        signed = done["countersign"]["receipt"]
        self.assertEqual(set(signed["body"]["approval"]),
                         {"hash", "ceiling", "currency", "approver", "approved_at", "expires_at"})
        check = receipt.verify(signed, self.merchant_key.public_key())
        self.assertTrue(check.ok, check.findings)
        self.assertFalse([f for f in check.findings if "passkey" in f])


class ApprovalFieldTest(unittest.TestCase):
    def setUp(self):
        merchant = FakeMerchant(FakeFx("84.10"))
        terms = merchant.offer({next(iter(merchant.catalog)): 1}, "INR", NOW)
        self.approval = Approval.grant(terms, terms.shopper_total, "acp:platform", NOW, NOW + timedelta(minutes=15))
        self.record = {"session_id": "cs_1", "origin": "https://shop.example", "rp_id": "shop.example",
                       "credential_id": "Y3JlZA", "public_key_spki_der": "AA", "authenticator_data": "AA",
                       "client_data_json": "AA", "signature": "AA"}

    def test_the_signature_is_not_a_term(self):
        signed = replace(self.approval, shopper_assertion=self.record)
        self.assertEqual(signed.hash, self.approval.hash)
        self.assertTrue(signed.is_intact())
        self.assertEqual(signed, self.approval)

    def test_wire_carries_it_only_when_present(self):
        self.assertNotIn("shopper_assertion", wire.approval_to_json(self.approval))
        self.assertIsNone(wire.approval_from_json(wire.approval_to_json(self.approval)).shopper_assertion)
        signed = replace(self.approval, shopper_assertion=self.record)
        back = wire.approval_from_json(json.loads(json.dumps(wire.approval_to_json(signed))))
        self.assertEqual(dict(back.shopper_assertion), self.record)


if __name__ == "__main__":
    unittest.main()
