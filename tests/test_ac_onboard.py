"""V10 live: scripts/ac_onboard.py, run with a fake Airwallex client and a fake host."""

import contextlib
import importlib.util
import io
import json
import pathlib
import unittest

from countersign.airwallex import AirwallexError

_PATH = pathlib.Path(__file__).resolve().parent.parent / "scripts" / "ac_onboard.py"
_spec = importlib.util.spec_from_file_location("ac_onboard", _PATH)
ac_onboard = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(ac_onboard)

API_KEY = "login-key-never-printed"
SECRET = "h" * 64
BASE = "https://shop.example"
ENV = {"AIRWALLEX_CLIENT_ID": "cid", "AIRWALLEX_API_KEY": API_KEY, "COUNTERSIGN_ACP_HMAC_SECRET": SECRET,
       "COUNTERSIGN_PUBLIC_URL": BASE}
SIGNED_UPLOAD_URL = "https://upload.example/slot?sig=never-printed"
CONFIG = {
    "merchant_id": "mer_1", "status": "ACTIVE",
    "psp_config": {"psp_name": "airwallex", "client_id": "cid", "api_key_configured": True, "managed_payment": True},
    "apis": {"create_checkout": BASE + "/acp/create_checkout", "secret_key_configured": True},
    "whitelisted_image_domains": [BASE],
    "mcp_endpoints": {"acp": "/pa/ai/mer_1/acp/mcp", "ucp": "/pa/ai/mer_1/ucp/mcp"},
    "product_upload": {"url": SIGNED_UPLOAD_URL, "method": "PUT", "headers": {"x-sig": "never-printed"},
                       "expires_at": "2026-11-02T10:00:00Z"},
}
NOT_ONBOARDED = AirwallexError(404, {"code": "MERCHANT_CONFIG_NOT_FOUND", "message": "no config"})
NO_ROUTE = AirwallexError(404, "Not Found")
SCOPE = AirwallexError(401, {"code": "unauthorized", "message": "Admin permissions required"})
DOCUMENTED, OTHER = "/pa/agentic_commerce/config", "/agentic-commerce/config"


class FakeClient:
    """Answers each config path on its own: a dict is a 200 body, an AirwallexError is raised."""

    def __init__(self, config=None, get_error=NOT_ONBOARDED, post_error=None, other=NO_ROUTE, login_error=None):
        self.answers = {DOCUMENTED: config if config is not None else get_error, OTHER: other}
        self.post_error, self.login_error = post_error, login_error
        self.gets, self.posts = [], []

    def token(self):
        if self.login_error:
            raise self.login_error
        return "token-never-printed"

    def get(self, path):
        self.gets.append(path)
        answer = self.answers[path]
        if isinstance(answer, AirwallexError):
            raise answer
        return answer

    def post(self, path, body):
        self.posts.append((path, body))
        if self.post_error:
            raise self.post_error
        return CONFIG


DISCOVERY = {"issuer": BASE, "authorization_endpoint": BASE + "/oauth/authorize",
             "token_endpoint": BASE + "/oauth/token", "registration_endpoint": BASE + "/oauth/register",
             "jwks_uri": BASE + "/.well-known/jwks.json", "code_challenge_methods_supported": ["S256"]}
JWKS = {"keys": [{"kty": "EC", "crv": "P-256", "kid": "k1", "x": "x", "y": "y"}]}


def host(acp_status=401, acp_body=None, page_status=200, seconds=0.3, discovery=None, jwks=None, jwks_seconds=0.1):
    """A fake host. With no discovery document the OAuth server answers 404, as before C14 is live."""
    calls = []

    def fetch(method, url, body):
        calls.append((method, url))
        if url.endswith("/acp/create_checkout"):
            return acp_status, {"type": "invalid_request", "code": "unauthorized"} if acp_body is None else acp_body, seconds
        if url.endswith("/.well-known/openid-configuration"):
            return (404, None, 0.1) if discovery is None else (200, discovery, 0.1)
        if url.endswith("/.well-known/jwks.json"):
            return (404, None, 0.1) if jwks is None else (200, jwks, jwks_seconds)
        return page_status, None, 0.1
    fetch.calls = calls
    return fetch


def run(argv, environ=None, client=None, fetch=None):
    out = io.StringIO()
    code = ac_onboard.main(argv, environ=dict(ENV) if environ is None else environ,
                           client_factory=(lambda: client) if client else None,
                           fetch=fetch or host(), out=out)
    return code, out.getvalue()


class ProbeTest(unittest.TestCase):
    def test_not_onboarded_yet(self):
        client = FakeClient()
        code, text = run([], client=client)
        self.assertEqual(code, 1)
        self.assertIn("onboarding API is live; this account is not onboarded yet", text)
        self.assertEqual(client.gets, [DOCUMENTED, OTHER])
        self.assertIn(f"GET /api/v1{OTHER} -> HTTP 404", text)

    def test_ready_wins_over_a_401_on_the_other_path(self):
        code, text = run([], client=FakeClient(other=SCOPE))
        self.assertEqual(code, 1)
        self.assertIn("not onboarded yet", text)

    def test_both_paths_missing_means_onboarding_is_not_on_the_account(self):
        code, text = run([], client=FakeClient(get_error=NO_ROUTE))
        self.assertEqual(code, 1)
        self.assertIn("onboarding API not on this account", text)

    def test_a_404_here_and_a_401_there_is_the_keys_scope(self):
        code, text = run([], client=FakeClient(get_error=NO_ROUTE, other=SCOPE))
        self.assertEqual(code, 2)
        self.assertIn("scope is too narrow", text)
        self.assertIn(f"GET /api/v1{OTHER} -> HTTP 401 unauthorized Admin permissions required", text)
        self.assertNotIn("not on this account", text)

    def test_a_401_on_the_documented_path_is_the_keys_scope(self):
        code, text = run([], client=FakeClient(get_error=SCOPE))
        self.assertEqual(code, 2)
        self.assertIn("Create a full-access key", text)
        self.assertNotIn(API_KEY, text)

    def test_a_refused_login_is_not_blamed_on_scope(self):
        client = FakeClient(login_error=AirwallexError(401, {"code": "credentials_invalid", "message": "bad key"}))
        code, text = run([], client=client)
        self.assertEqual(code, 2)
        self.assertIn("refused the login", text)
        self.assertEqual(client.gets, [])

    def test_an_answer_only_on_the_other_path_is_reported_not_guessed(self):
        code, text = run([], client=FakeClient(get_error=NO_ROUTE, other={"merchant_id": "mer_1"}))
        self.assertEqual(code, 1)
        self.assertIn("no clear verdict", text)
        self.assertIn(f"GET /api/v1{OTHER} -> HTTP 200", text)

    def test_onboarded_prints_the_allowlisted_fields_only(self):
        code, text = run([], client=FakeClient(config=CONFIG))
        self.assertEqual(code, 0)
        self.assertIn("merchant_id: mer_1", text)
        self.assertIn("mcp acp: /pa/ai/mer_1/acp/mcp", text)
        self.assertIn("product upload slot expires: 2026-11-02T10:00:00Z", text)
        self.assertIn(f"image domains: {BASE}", text)
        self.assertNotIn("never-printed", text)

    def test_without_keys_nothing_is_called(self):
        code, text = run([], environ={})
        self.assertEqual(code, 1)
        self.assertIn("nothing called", text)


class HostCheckTest(unittest.TestCase):
    def test_the_acp_door_and_a_product_page_pass(self):
        fetch = host()
        code, text = run(["--check-host"], fetch=fetch)
        self.assertEqual(code, 0, text)
        self.assertIn("POST /acp/create_checkout unsigned -> 401 from the ACP door", text)
        self.assertEqual(fetch.calls[0], ("POST", BASE + "/acp/create_checkout"))
        self.assertEqual(fetch.calls[1], ("GET", BASE + "/p/AUD-018-0"))

    def test_a_tap_refusal_means_acp_is_not_mounted(self):
        code, text = run(["--check-host"], fetch=host(acp_body={"refused": "missing Signature-Input"}))
        self.assertEqual(code, 1)
        self.assertIn("no COUNTERSIGN_ACP_HMAC_SECRET", text)

    def test_a_404_host_fails(self):
        code, text = run(["--check-host"], fetch=host(acp_status=404, acp_body={}, page_status=404))
        self.assertEqual(code, 1)
        self.assertIn("does not serve the edge's /acp routes", text)
        self.assertIn("catalogue links would not resolve", text)

    def test_no_oauth_server_yet_is_a_warning_only(self):
        code, text = run(["--check-host"])
        self.assertEqual(code, 0, text)
        self.assertIn("warning, the OAuth server is not served yet", text)

    def test_an_oauth_server_as_registered_passes(self):
        code, text = run(["--check-host"], fetch=host(discovery=DISCOVERY, jwks=JWKS))
        self.assertEqual(code, 0, text)
        self.assertIn("a JWKS with 1 key(s) in 0.10 s, as onboarding registers", text)
        self.assertNotIn("sign-in may fail", text)

    def test_a_slow_jwks_is_a_warning_only(self):
        # Airwallex rejected tokens when the JWKS took over 500 ms (#commerce-kits-9-and-10, 2026-11-04)
        for seconds, warned in ((0.5, False), (0.51, True), (22.6, True)):
            with self.subTest(seconds=seconds):
                code, text = run(["--check-host"], fetch=host(discovery=DISCOVERY, jwks=JWKS, jwks_seconds=seconds))
                self.assertEqual(code, 0, text)
                self.assertEqual("sign-in may fail" in text, warned, text)

    def test_an_oauth_server_that_disagrees_fails(self):
        cases = {
            "issuer is 'https://shop.example/'": {"issuer": BASE + "/"},  # one slash breaks Claude's check
            "jwks_uri is": {"jwks_uri": BASE + "/jwks"},
            "no registration_endpoint": {"registration_endpoint": None},
            "lacks S256": {"code_challenge_methods_supported": ["plain"]},
        }
        for said, change in cases.items():
            with self.subTest(said=said):
                code, text = run(["--check-host"], fetch=host(discovery={**DISCOVERY, **change}, jwks=JWKS))
                self.assertEqual(code, 1, text)
                self.assertIn(said, text)

    def test_a_jwks_with_no_keys_fails(self):
        for jwks in (None, {"keys": []}):
            with self.subTest(jwks=jwks):
                code, text = run(["--check-host"], fetch=host(discovery=DISCOVERY, jwks=jwks))
                self.assertEqual(code, 1, text)
                self.assertIn("every access token would be rejected", text)

    def test_an_oauth_mismatch_stops_the_onboard(self):
        client = FakeClient()
        code, text = run(["--onboard"], client=client, fetch=host(discovery={**DISCOVERY, "issuer": "x"}, jwks=JWKS))
        self.assertEqual((code, client.posts), (1, []))
        self.assertIn("nothing sent", text)

    def test_a_slow_answer_is_flagged(self):
        _, text = run(["--check-host"], fetch=host(seconds=31))
        self.assertIn("cold start", text)

    def test_the_host_must_be_https(self):
        with self.assertRaises(SystemExit), contextlib.redirect_stderr(io.StringIO()):
            run(["--check-host", "--base-url", "http://shop.example"])


class OnboardTest(unittest.TestCase):
    def test_a_dry_run_hides_both_secrets_and_calls_nothing(self):
        code, text = run(["--onboard", "--dry-run"], fetch=lambda *a: self.fail("no host call expected"))
        self.assertEqual(code, 0)
        body = json.loads(text.split("\n", 1)[1])
        self.assertEqual(body["apis"]["create_checkout"], BASE + "/acp/create_checkout")
        self.assertEqual(body["apis"]["secret_key"], "<set, 64 chars>")
        self.assertEqual(body["apis"]["oauth"]["acp"]["oauth_jwks"], BASE + "/.well-known/jwks.json")
        self.assertNotIn(SECRET, text)
        self.assertNotIn(API_KEY, text)

    def test_the_first_onboard_sends_both_sections(self):
        client = FakeClient()
        code, text = run(["--onboard"], client=client)
        self.assertEqual(code, 0, text)
        (path, body), = client.posts
        self.assertEqual(path, "/pa/agentic_commerce/onboard")
        self.assertEqual(body, {
            "psp_config": {"api_key_data": {"client_id": "cid", "api_key": API_KEY}},
            "apis": {"create_checkout": BASE + "/acp/create_checkout", "secret_key": SECRET,
                     "oauth": {"acp": {"oauth_base": BASE, "oauth_jwks": BASE + "/.well-known/jwks.json",
                                       "scopes": ["openid", "email"]}}},
            "whitelisted_image_domains": [BASE]})
        self.assertIn("PSP key: the login key", text)
        self.assertNotIn(SECRET, text)
        self.assertNotIn(API_KEY, text)

    def test_the_image_domain_is_scheme_and_host_only(self):
        for base in ("https://shop.example", "https://shop.example/", "https://shop.example:8443/shop"):
            with self.subTest(base=base):
                body, _ = ac_onboard.onboard_body(ENV, base)
                self.assertEqual(body["whitelisted_image_domains"],
                                 ["https://shop.example:8443" if "8443" in base else "https://shop.example"])
        body, _ = ac_onboard.onboard_body(ENV, BASE + "/")
        self.assertEqual(body["apis"]["oauth"]["acp"]["oauth_base"], BASE)  # the issuer, no trailing slash

    def test_a_separate_psp_key_is_used_when_set(self):
        client = FakeClient()
        env = {**ENV, "AIRWALLEX_PSP_CLIENT_ID": "psp", "AIRWALLEX_PSP_API_KEY": "psp-key"}
        run(["--onboard"], environ=env, client=client)
        self.assertEqual(client.posts[0][1]["psp_config"]["api_key_data"], {"client_id": "psp", "api_key": "psp-key"})

    def test_half_a_psp_key_pair_is_refused(self):
        env = {**ENV, "AIRWALLEX_PSP_CLIENT_ID": "psp"}
        with self.assertRaises(SystemExit), contextlib.redirect_stderr(io.StringIO()):
            run(["--onboard", "--dry-run"], environ=env)

    def test_no_secret_or_a_short_secret_is_refused(self):
        for secret in ("", "short"):
            env = {**ENV, "COUNTERSIGN_ACP_HMAC_SECRET": secret}
            with self.assertRaises(SystemExit), contextlib.redirect_stderr(io.StringIO()):
                run(["--onboard", "--dry-run"], environ=env)

    def test_a_host_that_is_not_ready_stops_the_onboard(self):
        client = FakeClient()
        code, text = run(["--onboard"], client=client, fetch=host(acp_body={"refused": "unsigned"}))
        self.assertEqual(code, 1)
        self.assertEqual(client.posts, [])
        self.assertIn("nothing sent", text)

    def test_already_onboarded_sends_nothing(self):
        client = FakeClient(config=CONFIG)
        code, text = run(["--onboard"], client=client)
        self.assertEqual((code, client.posts), (0, []))
        self.assertIn("already onboarded with this create_checkout", text)

    def test_a_different_url_points_to_config_update(self):
        client = FakeClient(config=CONFIG)
        code, text = run(["--onboard", "--base-url", "https://other.example"], client=client)
        self.assertEqual((code, client.posts), (1, []))
        self.assertIn("--update", text)

    def test_the_old_sessions_url_counts_as_different(self):
        old = {**CONFIG, "apis": {"create_checkout": BASE + "/acp/checkout_sessions", "secret_key_configured": True}}
        code, text = run(["--onboard"], client=FakeClient(config=old))
        self.assertEqual(code, 1)
        self.assertIn("create_checkout differs", text)

    def test_a_scope_problem_stops_the_onboard(self):
        client = FakeClient(get_error=NO_ROUTE, other=SCOPE)
        code, _ = run(["--onboard"], client=client)
        self.assertEqual((code, client.posts), (2, []))

    def test_an_onboard_refusal_exits_two(self):
        client = FakeClient(post_error=AirwallexError(400, {"code": "INVALID_REQUEST", "message": "bad url"}))
        code, text = run(["--onboard"], client=client)
        self.assertEqual(code, 2)
        self.assertIn("HTTP 400 INVALID_REQUEST bad url", text)


class UpdateTest(unittest.TestCase):
    """POST /config/update replaces the whole apis section, so it always carries all three parts."""

    APIS = {"create_checkout": BASE + "/acp/create_checkout", "secret_key": SECRET,
            "oauth": {"acp": {"oauth_base": BASE, "oauth_jwks": BASE + "/.well-known/jwks.json",
                              "scopes": ["openid", "email"]}}}

    def test_a_dry_run_hides_the_secret_and_calls_nothing(self):
        code, text = run(["--update", "--dry-run"], fetch=lambda *a: self.fail("no host call expected"))
        self.assertEqual(code, 0)
        self.assertIn("/pa/agentic_commerce/config/update", text)
        body = json.loads(text.split("\n", 1)[1])
        self.assertEqual(body, {"apis": {**self.APIS, "secret_key": "<set, 64 chars>"},
                                "whitelisted_image_domains": [BASE]})
        self.assertNotIn(SECRET, text)

    def test_an_onboarded_account_gets_the_whole_apis_section_and_never_the_psp_keys(self):
        client = FakeClient(config=CONFIG)
        code, text = run(["--update"], client=client)
        self.assertEqual(code, 0, text)
        (path, body), = client.posts
        self.assertEqual(path, "/pa/agentic_commerce/config/update")
        self.assertEqual(body, {"apis": self.APIS, "whitelisted_image_domains": [BASE]})
        self.assertIn("updated (apis and image domains; the PSP keys unchanged)", text)
        self.assertNotIn(SECRET, text)
        self.assertNotIn(API_KEY, text)

    def test_an_account_not_onboarded_is_not_updated(self):
        client = FakeClient()
        code, text = run(["--update"], client=client)
        self.assertEqual((code, client.posts), (1, []))
        self.assertIn("onboard first", text)

    def test_a_host_that_is_not_ready_stops_the_update(self):
        client = FakeClient(config=CONFIG)
        code, text = run(["--update"], client=client, fetch=host(acp_status=404, acp_body={}))
        self.assertEqual((code, client.posts), (1, []))
        self.assertIn("nothing sent", text)

    def test_an_update_refusal_exits_two(self):
        client = FakeClient(config=CONFIG, post_error=AirwallexError(400, {"code": "INVALID_REQUEST",
                                                                           "message": "oauth_jwks missing"}))
        code, text = run(["--update"], client=client)
        self.assertEqual(code, 2)
        self.assertIn("refused the update: HTTP 400 INVALID_REQUEST oauth_jwks missing", text)

    def test_onboard_and_update_together_are_refused(self):
        with self.assertRaises(SystemExit), contextlib.redirect_stderr(io.StringIO()):
            run(["--onboard", "--update", "--dry-run"])


try:
    from fastapi.testclient import TestClient

    from countersign import tap
    from countersign.edge import create_app
    from countersign.edge.acp import AcpConfig
    from countersign.fx import FakeFx
    from countersign.merchant import FakeMerchant
except ImportError:  # the server extra is not installed
    TestClient = None


@unittest.skipIf(TestClient is None, "needs the server extra: pip install -e '.[server]' httpx")
class RealEdgeTest(unittest.TestCase):
    """The host check reads the real edge's answers, not a guess at them."""

    def fetch_from(self, acp):
        client = TestClient(create_app(FakeMerchant(FakeFx("84.10")), tap.StaticDirectory({}),
                                       authority="testserver", acp=acp))

        def fetch(method, url, body):
            r = client.request(method, url.replace(BASE, ""), content=body,
                               headers={"content-type": "application/json"} if body else {})
            try:
                return r.status_code, r.json(), 0.0
            except ValueError:
                return r.status_code, None, 0.0
        return fetch

    def test_an_edge_with_the_acp_secret_passes(self):
        ok, lines = ac_onboard.check_host(BASE, self.fetch_from(AcpConfig(secret=b"k" * 32)))
        self.assertTrue(ok, lines)

    def test_an_edge_without_it_is_caught(self):
        ok, lines = ac_onboard.check_host(BASE, self.fetch_from(None))
        self.assertFalse(ok)
        self.assertIn("TAP door", lines[0])


if __name__ == "__main__":
    unittest.main()
