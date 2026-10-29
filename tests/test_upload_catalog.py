"""V4′: scripts/upload_catalog.py, run with a fake Airwallex client."""

import contextlib
import importlib.util
import io
import pathlib
import unittest

from countersign.airwallex import AirwallexError

_PATH = pathlib.Path(__file__).resolve().parent.parent / "scripts" / "upload_catalog.py"
_spec = importlib.util.spec_from_file_location("upload_catalog", _PATH)
upload_catalog = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(upload_catalog)

KEYS = {"AIRWALLEX_CLIENT_ID": "id", "AIRWALLEX_API_KEY": "secret-value-never-printed"}


class FakeClient:
    def __init__(self, reply=None, error=None):
        self.reply = reply
        self.error = error
        self.calls = []

    def post(self, path, body):
        self.calls.append((path, body))
        if self.error:
            raise self.error
        if self.reply is not None:
            return self.reply
        return {"records_added": len(body["gmc_products"]), "records_updated": 0, "records_failed": 0}


def run(argv, environ, client=None):
    out = io.StringIO()
    factory = (lambda: client) if client else (lambda: (_ for _ in ()).throw(AssertionError("no client expected")))
    code = upload_catalog.main(argv, environ=environ, client_factory=factory, out=out, sleep=lambda s: None)
    return code, out.getvalue()


class UploadCatalogTest(unittest.TestCase):
    def test_a_dry_run_calls_nothing(self):
        code, text = run(["--dry-run"], dict(KEYS))
        self.assertEqual(code, 0)
        self.assertIn("100 rows, 25 products, 1 call(s)", text)
        self.assertIn('"item_group_id"', text)

    def test_without_keys_it_falls_back_to_a_dry_run(self):
        code, text = run([], {})
        self.assertEqual(code, 0)
        self.assertIn("dry run only", text)

    def test_a_live_upload_needs_an_https_base_url(self):
        for argv in ([], ["--base-url", "http://shop.example"]):
            with self.assertRaises(SystemExit), contextlib.redirect_stderr(io.StringIO()):
                run(argv, dict(KEYS), FakeClient())

    def test_a_clean_upload_exits_zero(self):
        client = FakeClient()
        code, text = run(["--base-url", "https://shop.example"], dict(KEYS), client)
        self.assertEqual(code, 0)
        self.assertEqual(len(client.calls), 1)
        self.assertIn("total: added 100, updated 0, failed 0", text)
        self.assertTrue(client.calls[0][1]["gmc_products"][0]["link"].startswith("https://shop.example/p/"))

    def test_the_public_url_can_come_from_the_environment(self):
        client = FakeClient()
        code, _ = run([], {**KEYS, "COUNTERSIGN_PUBLIC_URL": "https://shop.example"}, client)
        self.assertEqual(code, 0)
        self.assertEqual(len(client.calls), 1)

    def test_any_failed_row_exits_one(self):
        client = FakeClient(reply={"records_added": 99, "records_updated": 0, "records_failed": 1})
        code, text = run(["--base-url", "https://shop.example"], dict(KEYS), client)
        self.assertEqual(code, 1)
        self.assertIn("failed 1", text)

    def test_a_refusal_exits_two_and_prints_no_secret(self):
        err = AirwallexError(401, {"code": "unauthorized", "message": "Admin permissions required"})
        code, text = run(["--base-url", "https://shop.example"], dict(KEYS), FakeClient(error=err))
        self.assertEqual(code, 2)
        self.assertIn("HTTP 401 unauthorized Admin permissions required", text)
        self.assertNotIn("secret-value-never-printed", text)

    def test_chunks_follow_the_limit_without_splitting_products(self):
        client = FakeClient()
        code, _ = run(["--base-url", "https://shop.example", "--limit", "10"], dict(KEYS), client)
        self.assertEqual(code, 0)
        self.assertEqual([len(b["gmc_products"]) for _, b in client.calls], [8] * 12 + [4])

    def test_extended_fields_are_opt_in(self):
        client = FakeClient()
        run(["--base-url", "https://shop.example", "--extended"], dict(KEYS), client)
        self.assertIn("brand", client.calls[0][1]["gmc_products"][0])


if __name__ == "__main__":
    unittest.main()
