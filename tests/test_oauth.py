"""C14 (W2): the merchant OAuth server, offline: discovery, registration, the passkey sign-in, PKCE, tokens."""

import base64
import hashlib
import importlib.util
import json
import pathlib
import secrets
import unittest
from urllib.parse import parse_qs, urlparse

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives.asymmetric.utils import encode_dss_signature

from countersign.edge.passkey import b64url, b64url_decode
from countersign.web import oauth

try:
    from fastapi.testclient import TestClient

    from countersign.web.host import Host, house_from_env
    from test_host import demo_app
except ImportError:  # the server extra is not installed
    TestClient = None

ISSUER = "https://shop.example"
REDIRECT = "http://localhost:3334/oauth/callback"
RESOURCE = "https://mcp.sandbox.airwallex.com/pa/ai/m1/acp/mcp"
KEY = oauth.new_signing_key()

_PATH = pathlib.Path(__file__).resolve().parent.parent / "scripts" / "ac_onboard.py"
_spec = importlib.util.spec_from_file_location("ac_onboard_for_oauth", _PATH)
ac_onboard = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(ac_onboard)


class Clock:
    def __init__(self, now=1_800_000_000.0):
        self.now = now

    def __call__(self):
        return self.now


class Device:
    """A passkey as navigator.credentials.get() uses it."""

    def __init__(self):
        self.key = ec.generate_private_key(ec.SECP256R1())
        self.credential_id = secrets.token_bytes(16)
        self.spki = self.key.public_key().public_bytes(serialization.Encoding.DER,
                                                       serialization.PublicFormat.SubjectPublicKeyInfo)

    def assertion(self, webauthn, *, origin=None):
        authenticator_data = hashlib.sha256(webauthn["rp_id"].encode()).digest() + b"\x05" + (1).to_bytes(4, "big")
        client_data_json = json.dumps({"type": "webauthn.get", "challenge": webauthn["challenge"],
                                       "origin": origin or webauthn["origin"], "crossOrigin": False}).encode()
        signature = self.key.sign(authenticator_data + hashlib.sha256(client_data_json).digest(),
                                  ec.ECDSA(hashes.SHA256()))
        return {"credential_id": b64url(self.credential_id), "public_key_spki_der": b64url(self.spki),
                "authenticator_data": b64url(authenticator_data), "client_data_json": b64url(client_data_json),
                "signature": b64url(signature)}


def pkce():
    verifier = secrets.token_urlsafe(48)
    return verifier, b64url(hashlib.sha256(verifier.encode()).digest())


def verify_jwt(token, jwks):
    """What Airwallex does with an access token: find the key by kid, check ES256. Returns the claims."""
    header_b64, claims_b64, sig_b64 = token.split(".")
    header = json.loads(b64url_decode(header_b64))
    jwk = next(k for k in jwks["keys"] if k["kid"] == header["kid"])
    assert header["alg"] == "ES256" and jwk["kty"] == "EC" and jwk["crv"] == "P-256"
    public = ec.EllipticCurvePublicNumbers(int.from_bytes(b64url_decode(jwk["x"]), "big"),
                                           int.from_bytes(b64url_decode(jwk["y"]), "big"),
                                           ec.SECP256R1()).public_key()
    raw = b64url_decode(sig_b64)
    signature = encode_dss_signature(int.from_bytes(raw[:32], "big"), int.from_bytes(raw[32:], "big"))
    public.verify(signature, f"{header_b64}.{claims_b64}".encode(), ec.ECDSA(hashes.SHA256()))
    return header, json.loads(b64url_decode(claims_b64))


class ServerTest(unittest.TestCase):
    def setUp(self):
        self.clock = Clock()
        self.server = oauth.server_from_env({"COUNTERSIGN_PUBLIC_URL": ISSUER + "/",
                                             "COUNTERSIGN_OAUTH_SIGNING_KEY": KEY}, clock=self.clock)
        self.client = self.server.register({"redirect_uris": [REDIRECT], "client_name": "Claude"})
        self.device = Device()

    def start(self, **overrides):
        verifier, challenge = pkce()
        params = {"response_type": "code", "client_id": self.client["client_id"], "redirect_uri": REDIRECT,
                  "code_challenge": challenge, "code_challenge_method": "S256", "state": "st-1",
                  "scope": "openid email", "resource": RESOURCE, **overrides}
        return verifier, self.server.start({k: v for k, v in params.items() if v is not None})

    def code(self, **overrides):
        verifier, started = self.start(**overrides)
        answer = self.server.sign_in({"request": started["request"], **self.device.assertion(started["webauthn"])})
        query = parse_qs(urlparse(answer["redirect"]).query)
        return verifier, query

    def exchange(self, **overrides):
        verifier, query = self.code()
        form = {"grant_type": "authorization_code", "code": query["code"][0], "redirect_uri": REDIRECT,
                "client_id": self.client["client_id"], "code_verifier": verifier, **overrides}
        return self.server.token(form)

    # discovery and keys

    def test_discovery_is_what_onboarding_registers(self):
        self.assertEqual(self.server.issuer, ISSUER)  # the trailing slash is dropped, as ac_onboard.oauth_base does
        self.assertEqual(ac_onboard.oauth_problems(self.server.metadata(), ISSUER + "/"), [])

    def test_the_jwks_holds_one_es256_key_onboarding_accepts(self):
        keys = self.server.jwks()["keys"]
        self.assertEqual(len(keys), 1)
        self.assertTrue(ac_onboard.token_key(keys[0]))
        self.assertNotIn("d", keys[0])  # never the private part

    def test_the_key_from_env_survives_a_restart(self):
        again = oauth.server_from_env({"COUNTERSIGN_PUBLIC_URL": ISSUER, "COUNTERSIGN_OAUTH_SIGNING_KEY": KEY})
        self.assertEqual(again.jwks(), self.server.jwks())
        self.assertIsNotNone(again.client(self.client["client_id"]))  # a registration outlives the process

    def test_a_bad_signing_key_stops_the_service(self):
        for bad in ("not base64!", base64.b64encode(b"short").decode(), base64.b64encode(b"\xff" * 32).decode()):
            with self.assertRaises(ValueError):
                oauth.signing_key_from({"COUNTERSIGN_OAUTH_SIGNING_KEY": bad})

    def test_no_public_url_means_no_oauth_server(self):
        self.assertIsNone(oauth.server_from_env({}))

    # registration

    def test_registration_answers_a_public_client(self):
        self.assertEqual(self.client["token_endpoint_auth_method"], "none")
        self.assertEqual(self.client["redirect_uris"], [REDIRECT])
        self.assertNotIn("client_secret", self.client)

    def test_registration_refuses_bad_redirect_uris(self):
        for uris in ([], ["http://evil.example/cb"], ["javascript:alert(1)"], ["https://x.example/cb#frag"], "x"):
            with self.assertRaises(oauth.OAuthError) as caught:
                self.server.register({"redirect_uris": uris})
            self.assertEqual(caught.exception.error, "invalid_redirect_uri")

    def test_registration_accepts_claude_web_and_loopback(self):
        for uri in ("https://claude.ai/api/mcp/auth_callback", "http://127.0.0.1:5555/cb"):
            self.assertEqual(self.server.register({"redirect_uris": [uri]})["redirect_uris"], [uri])

    def test_a_forged_client_id_is_unknown(self):
        body, mac = self.client["client_id"].split(".")
        forged = b64url(json.dumps({"redirect_uris": ["https://evil.example/cb"]}).encode()) + "." + mac
        self.assertIsNone(self.server.client(forged))
        self.assertIsNone(self.server.client("nonsense"))

    # authorize

    def test_unknown_client_or_redirect_never_redirects(self):
        for overrides in ({"client_id": "nope"}, {"redirect_uri": "https://evil.example/cb"}):
            with self.assertRaises(oauth.OAuthError) as caught:
                self.start(**overrides)
            self.assertFalse(hasattr(caught.exception, "redirect"))

    def test_pkce_s256_is_required(self):
        for overrides in ({"code_challenge": None}, {"code_challenge_method": "plain"},
                          {"code_challenge_method": None}, {"code_challenge": "short"}):
            with self.assertRaises(oauth.OAuthError) as caught:
                self.start(**overrides)
            redirect = parse_qs(urlparse(caught.exception.redirect).query)
            self.assertEqual(redirect["error"], ["invalid_request"])
            self.assertEqual(redirect["state"], ["st-1"])

    def test_the_passkey_challenge_is_for_this_request_on_our_origin(self):
        _, started = self.start()
        self.assertEqual(started["webauthn"]["origin"], ISSUER)
        self.assertEqual(started["webauthn"]["rp_id"], "shop.example")
        self.assertEqual(b64url_decode(started["webauthn"]["challenge"]), self.server.challenge(started["request"]))

    # sign-in

    def test_a_signed_in_shopper_goes_back_with_a_code_state_and_issuer(self):
        _, query = self.code()
        self.assertEqual(query["state"], ["st-1"])
        self.assertEqual(query["iss"], [ISSUER])
        self.assertIn("code", query)

    def test_a_passkey_from_another_origin_is_refused(self):
        _, started = self.start()
        with self.assertRaises(oauth.OAuthError) as caught:
            self.server.sign_in({"request": started["request"],
                                 **self.device.assertion(started["webauthn"], origin="https://evil.example")})
        self.assertEqual(caught.exception.error, "access_denied")

    def test_a_signature_for_another_request_is_refused(self):
        _, first = self.start()
        _, second = self.start()
        with self.assertRaises(oauth.OAuthError):
            self.server.sign_in({"request": second["request"], **self.device.assertion(first["webauthn"])})

    def test_a_sign_in_page_works_once(self):
        _, started = self.start()
        body = {"request": started["request"], **self.device.assertion(started["webauthn"])}
        self.server.sign_in(body)
        with self.assertRaises(oauth.OAuthError):
            self.server.sign_in(body)

    def test_an_expired_sign_in_page_is_refused(self):
        _, started = self.start()
        self.clock.now += oauth.REQUEST_TTL + 1
        with self.assertRaises(oauth.OAuthError):
            self.server.sign_in({"request": started["request"], **self.device.assertion(started["webauthn"])})

    def test_one_credential_cannot_change_its_key(self):
        _, started = self.start()
        self.server.sign_in({"request": started["request"], **self.device.assertion(started["webauthn"])})
        thief = Device()
        thief.credential_id = self.device.credential_id
        _, again = self.start()
        with self.assertRaises(oauth.OAuthError):
            self.server.sign_in({"request": again["request"], **thief.assertion(again["webauthn"])})

    # token

    def test_the_access_token_verifies_against_the_jwks(self):
        tokens = self.exchange()
        header, claims = verify_jwt(tokens["access_token"], self.server.jwks())
        self.assertEqual(header["typ"], "at+jwt")
        self.assertEqual(claims["iss"], ISSUER)
        self.assertEqual(claims["aud"], RESOURCE)
        self.assertEqual(claims["exp"] - claims["iat"], oauth.ACCESS_TTL)
        self.assertEqual(claims["sub"], oauth.subject_of(self.device.spki))
        self.assertNotIn("awx_customer_id", claims)  # a guest
        self.assertEqual(tokens["token_type"], "Bearer")

    def test_the_same_passkey_is_the_same_shopper(self):
        first = verify_jwt(self.exchange()["access_token"], self.server.jwks())[1]["sub"]
        second = verify_jwt(self.exchange()["access_token"], self.server.jwks())[1]["sub"]
        self.assertEqual(first, second)

    def test_a_tampered_token_fails_the_check(self):
        token = self.exchange()["access_token"]
        head, claims, sig = token.split(".")
        forged = json.loads(b64url_decode(claims)) | {"sub": "someone_else"}
        with self.assertRaises(InvalidSignature):
            verify_jwt(f"{head}.{b64url(json.dumps(forged).encode())}.{sig}", self.server.jwks())

    def test_the_id_token_is_for_the_client(self):
        _, claims = verify_jwt(self.exchange()["id_token"], self.server.jwks())
        self.assertEqual(claims["aud"], self.client["client_id"])

    def test_the_audience_falls_back_to_configuration(self):
        server = oauth.server_from_env({"COUNTERSIGN_PUBLIC_URL": ISSUER, "COUNTERSIGN_OAUTH_SIGNING_KEY": KEY,
                                        "COUNTERSIGN_OAUTH_AUDIENCE": RESOURCE}, clock=self.clock)
        self.server = server
        self.client = server.register({"redirect_uris": [REDIRECT]})
        verifier, query = self.code(resource=None)
        tokens = server.token({"grant_type": "authorization_code", "code": query["code"][0],
                               "client_id": self.client["client_id"], "code_verifier": verifier})
        self.assertEqual(verify_jwt(tokens["access_token"], server.jwks())[1]["aud"], RESOURCE)

    def test_a_wrong_verifier_is_refused(self):
        with self.assertRaises(oauth.OAuthError) as caught:
            self.exchange(code_verifier=secrets.token_urlsafe(48))
        self.assertEqual(caught.exception.error, "invalid_grant")

    def test_a_code_works_once(self):
        verifier, query = self.code()
        form = {"grant_type": "authorization_code", "code": query["code"][0], "client_id": self.client["client_id"],
                "code_verifier": verifier}
        self.server.token(form)
        with self.assertRaises(oauth.OAuthError) as caught:
            self.server.token(form)
        self.assertEqual(caught.exception.error, "invalid_grant")

    def test_a_code_expires(self):
        verifier, query = self.code()
        self.clock.now += oauth.CODE_TTL + 1
        with self.assertRaises(oauth.OAuthError):
            self.server.token({"grant_type": "authorization_code", "code": query["code"][0],
                               "client_id": self.client["client_id"], "code_verifier": verifier})

    def test_a_code_is_for_its_own_client_and_redirect(self):
        other = self.server.register({"redirect_uris": [REDIRECT]})
        with self.assertRaises(oauth.OAuthError):
            self.exchange(client_id=other["client_id"])
        with self.assertRaises(oauth.OAuthError):
            self.exchange(redirect_uri="http://localhost:9999/cb")

    def test_an_unknown_client_is_401(self):
        with self.assertRaises(oauth.OAuthError) as caught:
            self.exchange(client_id="nope")
        self.assertEqual((caught.exception.error, caught.exception.status), ("invalid_client", 401))

    def test_a_refresh_token_gives_a_new_access_token(self):
        tokens = self.exchange()
        self.clock.now += oauth.ACCESS_TTL + 5
        again = self.server.token({"grant_type": "refresh_token", "refresh_token": tokens["refresh_token"],
                                   "client_id": self.client["client_id"]})
        _, claims = verify_jwt(again["access_token"], self.server.jwks())
        self.assertEqual(claims["aud"], RESOURCE)
        self.assertGreater(claims["exp"], self.clock.now)

    def test_a_code_is_not_a_refresh_token(self):
        verifier, query = self.code()
        with self.assertRaises(oauth.OAuthError):
            self.server.token({"grant_type": "refresh_token", "refresh_token": query["code"][0],
                               "client_id": self.client["client_id"]})

    def test_other_grants_are_unsupported(self):
        with self.assertRaises(oauth.OAuthError) as caught:
            self.server.token({"grant_type": "client_credentials", "client_id": self.client["client_id"]})
        self.assertEqual(caught.exception.error, "unsupported_grant_type")


@unittest.skipIf(TestClient is None, "needs the server extra: pip install -e '.[server]' httpx")
class HttpTest(unittest.TestCase):
    """The whole sign-in over HTTP on the host, the way mcp-remote and the browser drive it."""

    def setUp(self):
        house = house_from_env({"COUNTERSIGN_PUBLIC_URL": ISSUER, "COUNTERSIGN_OAUTH_SIGNING_KEY": KEY})
        self.http = TestClient(Host(demo_app(), house), base_url=ISSUER)

    def test_discovery_and_jwks_are_public_and_cacheable(self):
        for path in (oauth.DISCOVERY_PATH, oauth.METADATA_PATH, oauth.JWKS_PATH):
            res = self.http.get(path)
            self.assertEqual(res.status_code, 200, path)
            self.assertEqual(res.headers["access-control-allow-origin"], "*")
            self.assertIn("max-age", res.headers["cache-control"])
        self.assertEqual(self.http.get(oauth.DISCOVERY_PATH).json()["issuer"], ISSUER)

    def test_check_host_passes_the_oauth_server(self):
        def fetch(method, url, body):
            res = self.http.request(method, url)
            return res.status_code, res.json(), 0.01

        ok, lines = ac_onboard.check_oauth(ISSUER, fetch)
        self.assertTrue(ok, lines)
        self.assertIn("as onboarding registers", lines[0])

    def test_the_full_sign_in(self):
        client = self.http.post(oauth.REGISTER_PATH, json={"redirect_uris": [REDIRECT], "client_name": "Claude"})
        self.assertEqual(client.status_code, 201)
        client_id = client.json()["client_id"]
        verifier, challenge = pkce()
        page = self.http.get(oauth.AUTHORIZE_PATH, params={
            "response_type": "code", "client_id": client_id, "redirect_uri": REDIRECT, "state": "abc",
            "code_challenge": challenge, "code_challenge_method": "S256", "resource": RESOURCE})
        self.assertEqual(page.status_code, 200)
        self.assertEqual(page.headers["x-frame-options"], "DENY")
        self.assertIn("Sign in with passkey", page.text)
        raw = page.text.split('id="signin-data">', 1)[1].split("</script>", 1)[0]
        self.assertNotIn("<", raw)
        data = json.loads(raw)
        self.assertEqual(data["client_name"], "Claude")
        answer = self.http.post(oauth.AUTHORIZE_PATH,
                                json={"request": data["request"], **Device().assertion(data["webauthn"])})
        self.assertEqual(answer.status_code, 200, answer.text)
        query = parse_qs(urlparse(answer.json()["redirect"]).query)
        tokens = self.http.post(oauth.TOKEN_PATH, data={
            "grant_type": "authorization_code", "code": query["code"][0], "redirect_uri": REDIRECT,
            "client_id": client_id, "code_verifier": verifier})
        self.assertEqual(tokens.status_code, 200, tokens.text)
        self.assertEqual(tokens.headers["cache-control"], "no-store")
        jwks = self.http.get(oauth.JWKS_PATH).json()
        self.assertEqual(verify_jwt(tokens.json()["access_token"], jwks)[1]["aud"], RESOURCE)

    def test_a_bad_authorize_shows_a_page_not_a_redirect(self):
        res = self.http.get(oauth.AUTHORIZE_PATH, params={"client_id": "nope"}, follow_redirects=False)
        self.assertEqual(res.status_code, 400)
        self.assertIn("cannot continue", res.text)

    def test_a_bad_pkce_redirects_back_with_the_error(self):
        client_id = self.http.post(oauth.REGISTER_PATH, json={"redirect_uris": [REDIRECT]}).json()["client_id"]
        res = self.http.get(oauth.AUTHORIZE_PATH, params={"response_type": "code", "client_id": client_id,
                                                          "redirect_uri": REDIRECT, "state": "s"},
                            follow_redirects=False)
        self.assertEqual(res.status_code, 302)
        self.assertTrue(res.headers["location"].startswith(REDIRECT + "?error=invalid_request"))

    def test_the_token_endpoint_answers_rfc_errors(self):
        res = self.http.post(oauth.TOKEN_PATH, data={"grant_type": "authorization_code", "client_id": "nope"})
        self.assertEqual(res.status_code, 401)
        self.assertEqual(res.json()["error"], "invalid_client")

    def test_no_public_url_serves_no_oauth(self):
        house = house_from_env({})
        http = TestClient(Host(demo_app(), house))
        self.assertIsNone(house.oauth)
        self.assertEqual(http.get(oauth.JWKS_PATH).status_code, 404)  # the demo answers, and has no such page


if __name__ == "__main__":
    unittest.main()
