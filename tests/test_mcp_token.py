"""A13: scripts/mcp_token.py puts the merchant MCP's bearer token in the shell, never on the screen."""

import importlib.util
import io
import pathlib
import shlex
import sys
import unittest
import unittest.mock

from countersign.airwallex import Airwallex, AirwallexError

_PATH = pathlib.Path(__file__).resolve().parent.parent / "scripts" / "mcp_token.py"
_spec = importlib.util.spec_from_file_location("mcp_token", _PATH)
mcp_token = importlib.util.module_from_spec(_spec)
sys.modules["mcp_token"] = mcp_token
_spec.loader.exec_module(mcp_token)

KEYS = {"AIRWALLEX_CLIENT_ID": "client-id", "AIRWALLEX_API_KEY": "the-api-key"}


class Pipe(io.StringIO):
    """stdout as `eval "$(...)"` sees it: not a terminal."""

    def isatty(self):
        return False


class Terminal(io.StringIO):
    def isatty(self):
        return True


def run(environ, out=None, send=None):
    out = out if out is not None else Pipe()
    err = io.StringIO()
    send = send or (lambda self, method, path, headers, body: {"token": "tok'en; rm -rf ~"})
    with unittest.mock.patch.object(Airwallex, "_send", send):
        code = mcp_token.main(environ, out, err)
    return code, out.getvalue(), err.getvalue()


class McpTokenTest(unittest.TestCase):
    def test_prints_one_export_line_the_shell_reads_back_exactly(self):
        code, out, err = run(KEYS)
        self.assertEqual(code, 0)
        self.assertEqual(out.count("\n"), 1)
        self.assertEqual(shlex.split(out), ["export", "AIRWALLEX_MCP_TOKEN=tok'en; rm -rf ~"])
        self.assertNotIn("tok'en", err)

    def test_logs_in_with_the_merchant_key_on_the_configured_host(self):
        seen = {}

        def send(self, method, path, headers, body):
            seen.update(url=self.base_url + path, headers=headers)
            return {"token": "t"}

        run({**KEYS, "AIRWALLEX_BASE_URL": "https://api-demo.airwallex.com",
             "AIRWALLEX_PSP_CLIENT_ID": "psp", "AIRWALLEX_PSP_API_KEY": "psp-key"}, send=send)
        self.assertEqual(seen["url"], "https://api-demo.airwallex.com/api/v1/authentication/login")
        self.assertEqual(seen["headers"], {"x-client-id": "client-id", "x-api-key": "the-api-key"})

    def test_refuses_to_print_the_token_to_a_terminal(self):
        called = []
        code, out, err = run(KEYS, out=Terminal(), send=lambda *a: called.append(a) or {"token": "t"})
        self.assertEqual((code, out, called), (2, "", []))
        self.assertIn("eval", err)

    def test_missing_keys_print_nothing_on_stdout(self):
        code, out, err = run({"AIRWALLEX_CLIENT_ID": "client-id"})
        self.assertEqual((code, out), (2, ""))
        self.assertIn("AIRWALLEX_API_KEY", err)

    def test_a_refused_login_names_the_status_not_the_key(self):
        def send(self, method, path, headers, body):
            raise AirwallexError(401, {"code": "credentials_invalid", "message": "the-api-key is wrong"})

        code, out, err = run(KEYS, send=send)
        self.assertEqual((code, out), (2, ""))
        self.assertIn("401 credentials_invalid", err)
        self.assertNotIn("the-api-key", err)


if __name__ == "__main__":
    unittest.main()
