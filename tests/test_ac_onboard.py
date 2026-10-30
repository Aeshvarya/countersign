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
    "apis": {"create_checkout": BASE + "/acp/checkout_sessions", "secret_key_configured": True},
    "mcp_endpoints": {"acp": "/pa/ai/mer_1/acp/mcp", "ucp": "/pa/ai/mer_1/ucp/mcp"},
    "product_upload": {"url": SIGNED_UPLOAD_URL, "method": "PUT", "headers": {"x-sig": "never-printed"},
                       "expires_at": "2026-11-02T10:00:00Z"},
}
NOT_ONBOARDED = AirwallexError(404, {"code": "MERCHANT_CONFIG_NOT_FOUND", "message": "no config"})


class FakeClient:
    def __init__(self, config=None, get_error=NOT_ONBOARDED, post_error=None):
        self.config, self.get_error, self.post_error = config, get_error, post_error
        self.posts = []

    def get(self, path):
        if self.config is not None:
            return self.config
        raise self.get_error

    def post(self, path, body):
        self.posts.append((path, body))
        if self.post_error:
            raise self.post_error
        return CONFIG


def host(acp_status=401, acp_body=None, page_status=200, seconds=0.3):
    calls = []

    def fetch(method, url, body):
        calls.append((method, url))
        if url.endswith("/acp/checkout_sessions"):
            return acp_status, {"type": "invalid_request", "code": "unauthorized"} if acp_body is None else acp_body, seconds
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
        code, text = run([], client=FakeClient())
        self.assertEqual(code, 1)
        self.assertIn("onboarding API is live; this account is not onboarded yet", text)

    def test_a_missing_route_means_onboarding_is_not_on_the_account(self):
        code, text = run([], client=FakeClient(get_error=AirwallexError(404, "Not Found")))
        self.assertEqual(code, 1)
        self.assertIn("onboarding API not on this account", text)
        self.assertIn("HTTP 404", text)

    def test_a_refusal_prints_code_and_message_only(self):
        err = AirwallexError(401, {"code": "unauthorized", "message": "Admin permissions required"})
        code, text = run([], client=FakeClient(get_error=err))
        self.assertEqual(code, 2)
        self.assertIn("HTTP 401 unauthorized Admin permissions required", text)
        self.assertNotIn(API_KEY, text)

    def test_onboarded_prints_the_allowlisted_fields_only(self):
        code, text = run([], client=FakeClient(config=CONFIG))
        self.assertEqual(code, 0)
        self.assertIn("merchant_id: mer_1", text)
        self.assertIn("mcp acp: /pa/ai/mer_1/acp/mcp", text)
        self.assertIn("product upload slot expires: 2026-11-02T10:00:00Z", text)
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
        self.assertIn("401 from the ACP door", text)
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
        self.assertEqual(body["apis"]["create_checkout"], BASE + "/acp/checkout_sessions")
        self.assertEqual(body["apis"]["secret_key"], "<set, 64 chars>")
        self.assertNotIn(SECRET, text)
        self.assertNotIn(API_KEY, text)

    def test_the_first_onboard_sends_both_sections(self):
        client = FakeClient()
        code, text = run(["--onboard"], client=client)
        self.assertEqual(code, 0, text)
        (path, body), = client.posts
        self.assertEqual(path, "/pa/agentic_commerce/onboard")
        self.assertEqual(body, {"psp_config": {"api_key_data": {"client_id": "cid", "api_key": API_KEY}},
                                "apis": {"create_checkout": BASE + "/acp/checkout_sessions", "secret_key": SECRET}})
        self.assertIn("PSP key: the login key", text)
        self.assertNotIn(SECRET, text)
        self.assertNotIn(API_KEY, text)

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

    def test_a_different_url_points_to_put_config(self):
        client = FakeClient(config=CONFIG)
        code, text = run(["--onboard", "--base-url", "https://other.example"], client=client)
        self.assertEqual((code, client.posts), (1, []))
        self.assertIn("PUT /config", text)

    def test_an_onboard_refusal_exits_two(self):
        client = FakeClient(post_error=AirwallexError(400, {"code": "INVALID_REQUEST", "message": "bad url"}))
        code, text = run(["--onboard"], client=client)
        self.assertEqual(code, 2)
        self.assertIn("HTTP 400 INVALID_REQUEST bad url", text)


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
