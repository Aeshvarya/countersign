import base64
import unittest

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from countersign import tap

AGENT = "https://agents.example"
NOW = 1_793_000_000


def setup(key=None):
    key = key or Ed25519PrivateKey.generate()
    directory = tap.StaticDirectory({(AGENT, "k1"): key.public_key()})
    return key, directory, tap.MemoryNonceCache()


def signed(key, path="/checkout", tag=tap.PAY, now=NOW, nonce="n-1", authority="shop.example", key_id="k1"):
    return tap.sign(key, authority=authority, path=path, key_id=key_id, signature_agent=AGENT, tag=tag, now=now, nonce=nonce)


def check(headers, directory, nonces, path="/checkout", now=NOW + 5, **kw):
    return tap.verify(headers, authority="shop.example", path=path, directory=directory, nonces=nonces, now=now, **kw)


class WireFormatTest(unittest.TestCase):
    def test_headers_match_visas_reference_format(self):
        key, _, _ = setup()
        s = signed(key)
        self.assertEqual(
            s.signature_input,
            'sig2=("@authority" "@path"); created=1793000000; expires=1793000480; '
            'keyId="k1"; alg="ed25519"; nonce="n-1"; tag="agent-payer-auth"',
        )
        self.assertTrue(s.signature.startswith("sig2=:") and s.signature.endswith(":"))
        self.assertEqual(s.headers()["Signature-Agent"], f'"{AGENT}"')

    def test_signature_base_is_the_three_lines_visa_signs(self):
        params = tap.signature_params(1, 2, "k1", "n", tap.PAY)
        self.assertEqual(
            tap.signature_base("shop.example", "/checkout", params),
            '"@authority": shop.example\n"@path": /checkout\n'
            '"@signature-params": ("@authority" "@path"); created=1; expires=2; keyId="k1"; alg="ed25519"; nonce="n"; tag="agent-payer-auth"',
        )

    def test_signature_is_raw_ed25519_over_the_base(self):
        key, _, _ = setup()
        s = signed(key)
        raw = base64.b64decode(s.signature[len("sig2=:"):-1])
        self.assertEqual(len(raw), 64)
        base = tap.signature_base("shop.example", "/checkout", s.signature_input[len("sig2="):])
        key.public_key().verify(raw, base.encode())  # raises if wrong


    def test_fixed_vector_checked_against_visas_reference_code(self):
        # Same inputs verified by Visa's cdn-proxy and tap-agent in scripts/tap_interop.py
        # (2026-10-11). Ed25519 is deterministic, so any change to the wire format breaks this.
        key = Ed25519PrivateKey.from_private_bytes(bytes(range(32)))
        s = tap.sign(key, authority="shop.example", path="/checkout", key_id="k1",
                     signature_agent="https://agents.example", tag=tap.PAY, now=1793000000, nonce="vector-1")
        self.assertEqual(
            s.signature,
            "sig2=:vt3Uuo7lVDi9+ctR1S+5JII2+u1ht60REWuHoLZO+6ODL+JmShSLpbfiVD4fPxZsZ8aQDcSWZAN5tRJMkbyhBw==:",
        )


class VerifyTest(unittest.TestCase):
    def test_a_signed_agent_is_accepted(self):
        key, d, n = setup()
        identity = check(signed(key).headers(), d, n)
        self.assertIsInstance(identity, tap.AgentIdentity)
        self.assertEqual((identity.key_id, identity.tag), ("k1", tap.PAY))

    def test_rejections_say_why(self):
        key, d, n = setup()
        cases = {
            "unsigned": (check({}, d, n), "unsigned"),
            "other page": (check(signed(key, path="/browse").headers(), d, n), "does not match"),
            "other merchant": (check(signed(key, authority="evil.example", nonce="n-2").headers(), d, n), "does not match"),
            "expired": (check(signed(key, nonce="n-3").headers(), d, n, now=NOW + 10_000), "expired"),
            "future": (check(signed(key, now=NOW + 3600, nonce="n-4").headers(), d, n), "future"),
            "unknown key": (check(signed(Ed25519PrivateKey.generate(), nonce="n-5", key_id="k9").headers(), d, n), "not in the directory"),
            "wrong key for a known id": (check(signed(Ed25519PrivateKey.generate(), nonce="n-7").headers(), d, n), "does not match"),
            "browse tag at checkout": (check(signed(key, tag=tap.BROWSE, nonce="n-6").headers(), d, n, require_tag=tap.PAY), "it needs agent-payer-auth"),
        }
        for name, (result, words) in cases.items():
            with self.subTest(name):
                self.assertIsInstance(result, tap.Rejection)
                self.assertIn(words, result.reason)

    def test_a_replayed_signature_is_refused(self):
        key, d, n = setup()
        headers = signed(key).headers()
        self.assertIsInstance(check(headers, d, n), tap.AgentIdentity)
        self.assertIn("already been used", check(headers, d, n).reason)

    def test_a_forged_request_does_not_burn_the_real_nonce(self):
        key, d, n = setup()
        forged = signed(Ed25519PrivateKey.generate()).headers()
        self.assertIsInstance(check(forged, d, n), tap.Rejection)
        self.assertIsInstance(check(signed(key).headers(), d, n), tap.AgentIdentity)

    def test_tampered_params_break_the_signature(self):
        key, d, n = setup()
        headers = signed(key).headers()
        headers["Signature-Input"] = headers["Signature-Input"].replace("expires=1793000480", "expires=1799999999")
        self.assertIn("does not match", check(headers, d, n).reason)

    def test_an_unreachable_directory_is_a_refusal_with_its_own_reason(self):
        class Down:
            def public_key(self, signature_agent, key_id):
                raise tap.DirectoryUnavailable("registry timed out")

        key, _, n = setup()
        result = check(signed(key).headers(), Down(), n)
        self.assertIsInstance(result, tap.Rejection)
        self.assertIn("could not be reached", result.reason)

    def test_keygen_round_trips(self):
        private, public = tap.new_keypair()
        key = tap.load_private_key(private)
        self.assertEqual(len(base64.b64decode(public)), 32)
        self.assertEqual(len(base64.b64decode(private)), 32)
        self.assertIsNotNone(key.public_key())


class BodyBindingTest(unittest.TestCase):
    def setUp(self):
        self.key, self.d, self.n = setup()

    def signed_with(self, body, nonce="b-1"):
        return tap.sign(self.key, authority="shop.example", path="/checkout", key_id="k1", signature_agent=AGENT,
                        tag=tap.PAY, now=NOW, nonce=nonce, body=body).headers()

    def check(self, headers, body, require_body=True):
        return check(headers, self.d, self.n, body=body, require_body=require_body)

    def test_a_signed_body_verifies(self):
        body = b'{"approval": {"ceiling": "10979.00"}}'
        headers = self.signed_with(body)
        self.assertEqual(headers["Content-Digest"], tap.content_digest(body))
        self.assertIn('"content-digest"', headers["Signature-Input"])
        self.assertIsInstance(self.check(headers, body), tap.AgentIdentity)

    def test_a_swapped_body_is_refused(self):
        headers = self.signed_with(b'{"ceiling": "10979.00"}')
        r = self.check(headers, b'{"ceiling": "99999.00"}')
        self.assertIn("body does not match", r.reason)

    def test_a_swapped_body_with_a_matching_digest_header_still_fails_the_signature(self):
        headers = self.signed_with(b'{"ceiling": "10979.00"}')
        forged = b'{"ceiling": "99999.00"}'
        headers["Content-Digest"] = tap.content_digest(forged)
        self.assertIn("does not match this merchant", self.check(headers, forged).reason)

    def test_a_covered_digest_needs_the_header(self):
        headers = self.signed_with(b"{}")
        del headers["Content-Digest"]
        self.assertIn("no Content-Digest header", self.check(headers, b"{}").reason)

    def test_a_payment_that_does_not_sign_its_body_is_refused_when_required(self):
        headers = signed(self.key).headers()  # Visa's two-component format, no body
        self.assertIn("must sign its body", self.check(headers, b"{}").reason)

    def test_without_a_body_the_format_is_still_exactly_visas(self):
        s = tap.sign(self.key, authority="shop.example", path="/checkout", key_id="k1", signature_agent=AGENT,
                     tag=tap.PAY, now=NOW, nonce="v")
        self.assertTrue(s.signature_input.startswith('sig2=("@authority" "@path"); created='))
        self.assertNotIn("Content-Digest", s.headers())


if __name__ == "__main__":
    unittest.main()
