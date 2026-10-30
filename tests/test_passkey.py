"""U1 / C13: the passkey verifier, against assertions built here the way a browser and authenticator build them."""

import hashlib
import json
import unittest
from datetime import datetime, timedelta, timezone

from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec, ed25519

from countersign.edge import passkey
from countersign.edge.passkey import b64url

ORIGIN = "https://shop.example"
RP_ID = "shop.example"
HASH = "ab" * 32
EXPIRES = datetime(2026, 11, 2, 10, 15, tzinfo=timezone.utc)
CHALLENGE = passkey.approval_challenge("cs_1", HASH, EXPIRES)


def spki(public_key) -> bytes:
    return public_key.public_bytes(serialization.Encoding.DER, serialization.PublicFormat.SubjectPublicKeyInfo)


class Device:
    """One passkey: an EC key pair, used the way navigator.credentials.get() uses it."""

    def __init__(self, curve=None):
        self.key = ec.generate_private_key(curve or ec.SECP256R1())
        self.public_key_spki_der = spki(self.key.public_key())

    def sign(self, challenge=CHALLENGE, *, origin=ORIGIN, rp_id=RP_ID, kind="webauthn.get", flags=0x05,
             sign_count=7, cross_origin=False, raw_challenge=None):
        authenticator_data = hashlib.sha256(rp_id.encode()).digest() + bytes([flags]) + sign_count.to_bytes(4, "big")
        client_data_json = json.dumps({
            "type": kind,
            "challenge": raw_challenge if raw_challenge is not None else b64url(challenge),
            "origin": origin,
            "crossOrigin": cross_origin,
        }).encode()
        signature = self.key.sign(authenticator_data + hashlib.sha256(client_data_json).digest(),
                                  ec.ECDSA(hashes.SHA256()))
        return {"public_key_spki_der": self.public_key_spki_der, "authenticator_data": authenticator_data,
                "client_data_json": client_data_json, "signature": signature}


def check(assertion, challenge=CHALLENGE):
    return passkey.verify_assertion(**assertion, expected_challenge=challenge, origin=ORIGIN, rp_id=RP_ID)


class VerifyAssertionTest(unittest.TestCase):
    def setUp(self):
        self.device = Device()

    def test_a_valid_assertion_passes(self):
        self.assertEqual(check(self.device.sign()), passkey.Verified(user_verified=True, sign_count=7))

    def test_user_verified_is_recorded_not_required(self):
        result = check(self.device.sign(flags=passkey.FLAG_USER_PRESENT))
        self.assertEqual(result, passkey.Verified(user_verified=False, sign_count=7))

    # 1. the kind of ceremony
    def test_a_registration_is_not_an_approval(self):
        self.assertEqual(check(self.device.sign(kind="webauthn.create")), "clientDataJSON type is not webauthn.get")

    def test_client_data_must_be_a_json_object(self):
        for raw, reason in ((b"not json", "clientDataJSON is not valid JSON"),
                            (b"\xff\xfe", "clientDataJSON is not valid JSON"),
                            (b"[1]", "clientDataJSON is not a JSON object")):
            a = self.device.sign()
            a["client_data_json"] = raw
            self.assertEqual(check(a), reason)

    # 2. the challenge: these terms, this session, this expiry
    def test_a_signature_for_another_session_is_refused(self):
        other = passkey.approval_challenge("cs_2", HASH, EXPIRES)
        self.assertEqual(check(self.device.sign(other)), "the challenge is not for these terms")

    def test_a_signature_over_different_terms_is_refused(self):
        other = passkey.approval_challenge("cs_1", "cd" * 32, EXPIRES)
        self.assertEqual(check(self.device.sign(other)), "the challenge is not for these terms")

    def test_a_signature_for_another_expiry_is_refused(self):
        other = passkey.approval_challenge("cs_1", HASH, EXPIRES + timedelta(minutes=15))
        self.assertEqual(check(self.device.sign(other)), "the challenge is not for these terms")

    def test_the_bare_terms_hash_is_not_the_challenge(self):
        self.assertEqual(check(self.device.sign(bytes.fromhex(HASH))), "the challenge is not for these terms")

    def test_a_challenge_that_is_not_base64url_is_refused(self):
        for raw in ("", "a+b/", 42):
            self.assertEqual(check(self.device.sign(raw_challenge=raw)), "the challenge is missing or not base64url")

    # 3. the origin
    def test_another_origin_is_refused(self):
        for origin in ("https://evil.example", "http://shop.example", "https://shop.example:8443"):
            self.assertEqual(check(self.device.sign(origin=origin)), "the origin does not match", origin)

    def test_a_cross_origin_frame_is_refused(self):
        self.assertEqual(check(self.device.sign(cross_origin=True)), "the assertion was made in a cross-origin frame")

    # 4. the relying party
    def test_a_credential_for_another_site_is_refused(self):
        self.assertEqual(check(self.device.sign(rp_id="evil.example")), "the RP id hash does not match")

    def test_short_authenticator_data_is_refused(self):
        a = self.device.sign()
        a["authenticator_data"] = a["authenticator_data"][:36]
        self.assertEqual(check(a), "authenticatorData is too short")

    # 5. a person was there
    def test_without_user_presence_it_is_refused(self):
        self.assertEqual(check(self.device.sign(flags=passkey.FLAG_USER_VERIFIED)), "the user-present flag is not set")

    # 6. the key and the signature
    def test_tampered_authenticator_data_is_refused(self):
        a = self.device.sign()
        a["authenticator_data"] = a["authenticator_data"][:36] + b"\x08"  # the sign count, after signing
        self.assertEqual(check(a), "the signature does not verify")

    def test_client_data_is_hashed_as_received_never_reserialised(self):
        a = self.device.sign()
        a["client_data_json"] = json.dumps(json.loads(a["client_data_json"]), indent=1).encode()  # same fields
        self.assertEqual(check(a), "the signature does not verify")

    def test_another_devices_key_is_refused(self):
        a = self.device.sign()
        a["public_key_spki_der"] = Device().public_key_spki_der
        self.assertEqual(check(a), "the signature does not verify")

    def test_a_malformed_signature_is_refused(self):
        a = self.device.sign()
        a["signature"] = b"\x30\x02\x01\x01"
        self.assertEqual(check(a), "the signature does not verify")

    def test_only_p256_keys_are_accepted(self):
        p384 = Device(ec.SECP384R1()).sign()
        self.assertEqual(check(p384), "the public key is not ECDSA P-256")
        a = self.device.sign()
        a["public_key_spki_der"] = spki(ed25519.Ed25519PrivateKey.generate().public_key())
        self.assertEqual(check(a), "the public key is not ECDSA P-256")
        a["public_key_spki_der"] = b"not a key"
        self.assertEqual(check(a), "the public key is not valid SPKI DER")


class ChallengeTest(unittest.TestCase):
    def test_thirty_two_bytes_that_depend_on_all_three_inputs(self):
        self.assertEqual(len(CHALLENGE), 32)
        self.assertEqual(CHALLENGE, passkey.approval_challenge("cs_1", HASH, EXPIRES))
        same_instant = EXPIRES.astimezone(timezone(timedelta(hours=5, minutes=30)))
        self.assertEqual(CHALLENGE, passkey.approval_challenge("cs_1", HASH, same_instant))

    def test_a_naive_expiry_is_refused(self):
        with self.assertRaises(ValueError):
            passkey.approval_challenge("cs_1", HASH, EXPIRES.replace(tzinfo=None))


class Base64UrlTest(unittest.TestCase):
    def test_round_trip_with_or_without_padding(self):
        for raw in (b"", b"a", b"ab", b"\xfb\xff"):
            text = b64url(raw)
            self.assertNotIn("=", text)
            if raw:
                self.assertEqual(passkey.b64url_decode(text), raw)
                self.assertEqual(passkey.b64url_decode(text + "=" * (-len(text) % 4)), raw)

    def test_strict_alphabet(self):
        for bad in ("", "+/8", "a b", "a.b", None, 5, "a"):
            with self.assertRaises(ValueError, msg=repr(bad)):
                passkey.b64url_decode(bad)


if __name__ == "__main__":
    unittest.main()
