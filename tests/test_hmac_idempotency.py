"""V10: the platform HMAC check and the ACP idempotency rules, without HTTP."""

import unittest

from countersign.edge import hmac_auth
from countersign.edge.idempotency import IdempotencyStore, Stored, fingerprint

SECRET = b"s" * 32
NOW = 1_793_000_000.0
BODY = b'{"items":[{"id":"AUD-018-0","quantity":1}]}'


class HmacTest(unittest.TestCase):
    def test_each_scheme_accepts_its_own_signature(self):
        for scheme in hmac_auth.SCHEMES:
            headers = hmac_auth.sign(BODY, SECRET, scheme=scheme, now=NOW)
            self.assertIsNone(hmac_auth.verify(headers, BODY, SECRET, scheme=scheme, now=NOW + 1), scheme)

    def test_one_changed_byte_is_refused(self):
        for scheme in hmac_auth.SCHEMES:
            headers = hmac_auth.sign(BODY, SECRET, scheme=scheme, now=NOW)
            tampered = BODY.replace(b"1}", b"2}")
            self.assertEqual(hmac_auth.verify(headers, tampered, SECRET, scheme=scheme, now=NOW),
                             "the signature does not match", scheme)

    def test_the_wrong_secret_is_refused(self):
        headers = hmac_auth.sign(BODY, SECRET, now=NOW)
        self.assertEqual(hmac_auth.verify(headers, BODY, b"other", now=NOW), "the signature does not match")

    def test_stale_and_future_signatures_are_refused(self):
        for scheme in hmac_auth.SCHEMES:
            headers = hmac_auth.sign(BODY, SECRET, scheme=scheme, now=NOW)
            self.assertEqual(hmac_auth.verify(headers, BODY, SECRET, scheme=scheme, now=NOW + 301),
                             "the signature is too old", scheme)
            self.assertEqual(hmac_auth.verify(headers, BODY, SECRET, scheme=scheme, now=NOW - 301),
                             "the signature is dated in the future", scheme)
            self.assertIsNone(hmac_auth.verify(headers, BODY, SECRET, scheme=scheme, now=NOW + 299), scheme)

    def test_seconds_where_milliseconds_belong_are_refused(self):
        ts = str(int(NOW))
        headers = {"x-timestamp": ts, "x-signature": hmac_auth._mac(SECRET, ts.encode() + BODY).hex()}
        self.assertEqual(hmac_auth.verify(headers, BODY, SECRET, now=NOW), "x-timestamp is not in milliseconds")

    def test_a_scheme_does_not_accept_another_schemes_headers(self):
        headers = hmac_auth.sign(BODY, SECRET, scheme="acp", now=NOW)
        self.assertEqual(hmac_auth.verify(headers, BODY, SECRET, scheme="airwallex", now=NOW),
                         "missing x-timestamp or x-signature")

    def test_header_names_are_case_insensitive(self):
        headers = {k.upper(): v for k, v in hmac_auth.sign(BODY, SECRET, now=NOW).items()}
        self.assertIsNone(hmac_auth.verify(headers, BODY, SECRET, now=NOW))

    def test_no_secret_means_no_entry(self):
        headers = hmac_auth.sign(BODY, SECRET, now=NOW)
        self.assertEqual(hmac_auth.verify(headers, BODY, b"", now=NOW), "no shared secret is configured")

    def test_odd_characters_in_a_header_are_refused_not_a_crash(self):
        # A 500 here would tell a prober it found a crash; it must read as any other bad signature.
        stamp = {"airwallex": "x-timestamp", "guess": "Timestamp"}
        mark = {"airwallex": "x-signature", "guess": "Signature"}
        for scheme in hmac_auth.SCHEMES:
            good = hmac_auth.sign(BODY, SECRET, scheme=scheme, now=NOW)
            if scheme == "acp":
                t = good["Merchant-Signature"].split(",")[0]
                odd = [{"Merchant-Signature": f"{t},v1={'é' * 64}"}, {"Merchant-Signature": "t=1793000²00,v1=ab"}]
            else:
                odd = [{**good, mark[scheme]: "é" * 64}, {**good, stamp[scheme]: good[stamp[scheme]][:-1] + "²"}]
            for headers in odd:
                self.assertIsInstance(hmac_auth.verify(headers, BODY, SECRET, scheme=scheme, now=NOW), str,
                                      (scheme, headers))

    def test_an_unknown_scheme_is_a_configuration_error(self):
        with self.assertRaises(ValueError):
            hmac_auth.verify({}, BODY, SECRET, scheme="any")


class IdempotencyTest(unittest.TestCase):
    def setUp(self):
        self.now = 1000.0
        self.store = IdempotencyStore(clock=lambda: self.now)
        self.fp = fingerprint("POST", "/acp/checkout_sessions", {"items": [1]})

    def test_a_finished_request_replays(self):
        self.assertEqual(self.store.begin("k", self.fp), ("new", None))
        self.store.finish("k", Stored(201, {"id": "cs_1"}, {}))
        state, stored = self.store.begin("k", self.fp)
        self.assertEqual((state, stored.body), ("replay", {"id": "cs_1"}))

    def test_the_same_key_for_a_different_request_conflicts(self):
        self.store.begin("k", self.fp)
        self.store.finish("k", Stored(201, {}, {}))
        other = fingerprint("POST", "/acp/checkout_sessions", {"items": [2]})
        self.assertEqual(self.store.begin("k", other), ("conflict", None))

    def test_a_running_request_is_in_flight(self):
        self.store.begin("k", self.fp)
        self.assertEqual(self.store.begin("k", self.fp), ("in_flight", None))

    def test_server_errors_are_never_stored(self):
        self.store.begin("k", self.fp)
        self.store.finish("k", Stored(503, {}, {}))
        self.assertEqual(self.store.begin("k", self.fp), ("new", None))

    def test_an_abandoned_request_can_be_retried(self):
        self.store.begin("k", self.fp)
        self.store.abandon("k")
        self.assertEqual(self.store.begin("k", self.fp), ("new", None))

    def test_keys_are_kept_a_day_then_forgotten(self):
        self.store.begin("k", self.fp)
        self.store.finish("k", Stored(200, {}, {}))
        self.now += 24 * 3600 - 1
        self.assertEqual(self.store.begin("k", self.fp)[0], "replay")
        self.now += 2
        self.assertEqual(self.store.begin("k", self.fp)[0], "new")

    def test_equivalent_json_shares_a_fingerprint(self):
        a = fingerprint("POST", "/p", {"a": 1, "b": [1, 2]})
        self.assertEqual(a, fingerprint("POST", "/p", {"b": [1, 2], "a": 1}))
        self.assertNotEqual(a, fingerprint("POST", "/q", {"a": 1, "b": [1, 2]}))


if __name__ == "__main__":
    unittest.main()
