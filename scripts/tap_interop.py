"""Check our TAP signatures against Visa's own reference code, both directions.

Needs a clone of github.com/visa/trusted-agent-protocol with `npm install` run
in its cdn-proxy folder:

    python scripts/tap_interop.py /path/to/trusted-agent-protocol

1. Ours → Visa: starts Visa's cdn-proxy (port 3001) with a stub key registry
   (9002) and a stub page (3000), registers our public key, and sends one signed
   and three bad requests through it. The proxy only forwards a request it has
   verified, so a 200 from the stub page means Visa's verifier accepted us.
2. Visa → ours: loads `create_ed25519_signature` from Visa's tap-agent source,
   signs with it, and verifies the result with countersign.tap.verify.
"""

from __future__ import annotations

import ast
import base64
import json
import os
import subprocess
import sys
import threading
import time
import types
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

from countersign import tap

KEY_ID = "countersign-interop-1"
AGENT_DIR = "https://agents.countersign.dev"


def serve(port: int, handler: type[BaseHTTPRequestHandler]) -> HTTPServer:
    server = HTTPServer(("127.0.0.1", port), handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server


def quiet(cls):
    cls.log_message = lambda *a, **k: None
    return cls


def registry_handler(public_b64: str):
    @quiet
    class Registry(BaseHTTPRequestHandler):
        def do_GET(self):
            if self.path != f"/keys/{KEY_ID}":
                self.send_response(404)
                self.end_headers()
                return
            body = json.dumps({"key_id": KEY_ID, "algorithm": "ed25519", "public_key": public_b64, "is_active": "true"}).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(body)

    return Registry


@quiet
class Page(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.end_headers()
        self.wfile.write(b"merchant page reached")


def get(url: str, headers: dict[str, str]) -> int:
    req = urllib.request.Request(url, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            return resp.status
    except urllib.error.HTTPError as err:
        return err.code


def ours_to_visa(visa: Path, private_b64: str, public_b64: str) -> bool:
    key = tap.load_private_key(private_b64)
    registry = serve(9002, registry_handler(public_b64))
    page = serve(3000, Page)
    proxy = subprocess.Popen(["node", "server.js"], cwd=visa / "cdn-proxy", env={**os.environ, "PORT": "3001"},
                             stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        for _ in range(50):
            try:
                urllib.request.urlopen("http://127.0.0.1:3001/test-proxy", timeout=1)
                break
            except Exception:
                time.sleep(0.2)
        authority, path = "localhost:3001", "/product/42"

        def signed(nonce: str, sign_path: str = path, now: int | None = None) -> dict[str, str]:
            s = tap.sign(key, authority=authority, path=sign_path, key_id=KEY_ID, signature_agent=AGENT_DIR,
                         tag=tap.BROWSE, nonce=nonce, now=now)
            return s.headers()

        url = f"http://{authority}{path}"
        good = signed("interop-good")
        cases = [
            ("our signature", good, 200),
            ("the same signature again (replay)", good, 403),
            ("signed for another page", signed("interop-path", sign_path="/product/7"), 403),
            ("no signature (bot)", {}, 403),
        ]
        ok = True
        for name, headers, want in cases:
            time.sleep(0.05)  # the proxy's key cache lives 5 ms
            got = get(url, headers)
            ok &= got == want
            print(f"  {'PASS' if got == want else 'FAIL'}  Visa's cdn-proxy, {name}: HTTP {got} (want {want})")
        return ok
    finally:
        proxy.terminate()
        registry.shutdown()
        page.shutdown()


def visa_to_ours(visa: Path, private_b64: str, public_b64: str) -> bool:
    source = (visa / "tap-agent" / "agent_app.py").read_text()
    tree = ast.parse(source)
    fn = next(n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == "create_ed25519_signature")
    namespace: dict = {
        "base64": base64,
        "st": types.SimpleNamespace(error=lambda *a, **k: None),
        "get_ed25519_keys_from_env": lambda: (private_b64, public_b64),
        "print": lambda *a, **k: None,
    }
    exec(compile(ast.Module(body=[fn], type_ignores=[]), "visa_tap_agent", "exec"), namespace)
    now = int(time.time())
    signature_input, signature = namespace["create_ed25519_signature"](
        "", "shop.example", "/checkout", KEY_ID, "visa-nonce-1", now, now + 300, tap.PAY)
    directory = tap.StaticDirectory({(AGENT_DIR, KEY_ID): tap.load_private_key(private_b64).public_key()})
    result = tap.verify(
        {"Signature-Agent": f'"{AGENT_DIR}"', "Signature-Input": signature_input, "Signature": signature},
        authority="shop.example", path="/checkout", directory=directory, nonces=tap.MemoryNonceCache(),
    )
    ok = isinstance(result, tap.AgentIdentity)
    print(f"  {'PASS' if ok else 'FAIL'}  Visa's tap-agent signer → countersign.tap.verify: {result}")
    return ok


def main() -> int:
    visa = Path(sys.argv[1]).resolve()
    private_b64, public_b64 = tap.new_keypair()
    print("Ours → Visa")
    a = ours_to_visa(visa, private_b64, public_b64)
    print("Visa → ours")
    b = visa_to_ours(visa, private_b64, public_b64)
    print("INTEROP OK" if a and b else "INTEROP FAILED")
    return 0 if a and b else 1


if __name__ == "__main__":
    sys.exit(main())
