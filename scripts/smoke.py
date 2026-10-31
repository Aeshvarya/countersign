"""Smoke test for the deployed host: does every door answer as designed?

    python scripts/smoke.py --base-url https://countersign-demo.onrender.com
    set -a; . ./.env; set +a; python scripts/smoke.py --base-url https://... --buy

Without secrets it checks what anyone can see: the health check, the receipt
key, the catalogue pages, the shopper and /verify pages and their headers, and
that unsigned calls are refused at the TAP door and at /acp. Each secret found
in the environment unlocks more, and nothing secret is ever printed:

    COUNTERSIGN_ACP_HMAC_SECRET   create a checkout as Airwallex would, read it, cancel it
    COUNTERSIGN_MCP_TOKEN         our fallback MCP: initialize, list tools, search
    COUNTERSIGN_LEVER_TOKEN       the merchant console's API

--buy (needs the HMAC secret) runs one purchase: it opens a checkout, prints the
shopper's link and waits while you approve (and pay, with the Airwallex
merchant), completes it, and checks the signed receipt here against the
merchant's published key. Run it right after a deploy, and before recording.

Exit 0 when every check passed, 1 otherwise.
"""

from __future__ import annotations

import argparse
import base64
import json
import os
import sys
import time
from dataclasses import dataclass
from typing import Any, Callable
from urllib.parse import urlsplit

from countersign import receipt as receipts
from countersign.merchant import synthetic_catalog

SESSIONS = "/acp/checkout_sessions"


@dataclass
class Check:
    name: str
    ok: bool | None  # None: skipped
    detail: str = ""


class Smoke:
    def __init__(self, client: Any, base_url: str, *, secret: str = "", mcp_token: str = "", lever_token: str = "",
                 out: Callable[[str], None] = print, sleep: Callable[[float], None] = time.sleep) -> None:
        self.client = client
        self.base = base_url.rstrip("/")
        self.secret = secret.encode()
        self.mcp_token = mcp_token
        self.lever_token = lever_token
        self.out = out
        self.sleep = sleep
        self.checks: list[Check] = []
        self.health: dict[str, Any] = {}
        self.key: dict[str, Any] = {}

    def record(self, name: str, ok: bool | None, detail: str = "") -> bool:
        self.checks.append(Check(name, ok, detail))
        mark = {True: "PASS", False: "FAIL", None: "SKIP"}[ok]
        self.out(f"{mark}  {name}" + (f": {detail}" if detail else ""))
        return bool(ok)

    def acp(self, method: str, path: str, body: dict[str, Any] | None = None, key: str | None = None):
        from countersign.edge import hmac_auth

        raw = json.dumps(body).encode() if body is not None else b""
        headers = {**hmac_auth.sign(raw, self.secret), "api-version": "2026-04-17"}
        if body is not None:
            headers["content-type"] = "application/json"
        if method == "POST":
            headers["idempotency-key"] = key or f"smoke-{time.time_ns()}"
        return self.client.request(method, path, content=raw, headers=headers)

    # ------------------------------------------------------------ public doors

    def public(self) -> None:
        started = time.monotonic()
        r = self.client.get("/healthz")
        waited = time.monotonic() - started
        if not self.record("health check", r.status_code == 200, f"{r.status_code} in {waited:.1f}s"
                           + (" (it was asleep: keep the uptime monitor on)" if waited > 10 else "")):
            return
        self.health = r.json()
        host = urlsplit(self.base).netloc
        same = self.health.get("authority") == host
        self.record("TAP authority is this host", same,
                    str(self.health.get("authority")) + ("" if same else f" (set COUNTERSIGN_PUBLIC_URL to {self.base})"))
        self.record("create_checkout is mounted", bool(self.health.get("acp")),
                    "set COUNTERSIGN_ACP_HMAC_SECRET" if not self.health.get("acp") else "")

        r = self.client.get("/.well-known/countersign-merchant-key")
        ok = r.status_code == 200 and r.json().get("alg") == "ed25519"
        if ok:
            self.key = r.json()
        self.record("receipt key published", ok, self.key.get("key_id", str(r.status_code)))

        sku = synthetic_catalog()[0].sku
        page, img = self.client.get(f"/p/{sku}"), self.client.get(f"/img/{sku}.svg")
        self.record("catalogue page and image", page.status_code == 200 and img.status_code == 200
                    and "svg" in img.headers.get("content-type", ""), f"/p/{sku} {page.status_code}, image {img.status_code}")

        r = self.client.post("/checkout", json={"approval": {}, "idempotency_key": "smoke"})
        self.record("TAP door refuses an unsigned agent", r.status_code == 401 and "refused" in r.json(),
                    r.json().get("refused", str(r.status_code)) if r.status_code == 401 else str(r.status_code))

        r = self.client.post(SESSIONS, json={"items": []})
        self.record("create_checkout refuses an unsigned caller", r.status_code == 401, str(r.status_code))

        page = self.client.get("/approve/cs_smoke")
        csp = page.headers.get("content-security-policy", "")
        self.record("shopper page served, not frameable", page.status_code == 200 and "frame-ancestors 'none'" in csp
                    and page.headers.get("referrer-policy") == "strict-origin", str(page.status_code))
        self.record("receipt checker served", self.client.get("/verify").status_code == 200)

    # ------------------------------------------------------------ with secrets

    def platform(self) -> str | None:
        """Create, read and cancel one checkout as Airwallex would. Returns nothing charged, ever."""
        if not self.secret:
            self.record("checkout as Airwallex calls it", None, "COUNTERSIGN_ACP_HMAC_SECRET not set here")
            return None
        sku = next(p.sku for p in synthetic_catalog() if p.stock > 0)
        r = self.acp("POST", SESSIONS, {"items": [{"id": sku, "quantity": 1}], "currency": "INR"})
        if not self.record("create_checkout with the HMAC secret", r.status_code == 201, str(r.status_code)
                           + ("" if r.status_code != 401 else ": the secret here is not the one on the host")):
            return None
        view = r.json()
        sid = view["id"]
        total = next((t["amount"] for t in view["totals"] if t["type"] == "total"), None)
        self.record("priced with a rate lock", total is not None and bool(view.get("countersign", {}).get("rate_lock_id")),
                    f"total {total} minor units, lock {view.get('countersign', {}).get('rate_lock_id')}")
        approval = next((link["url"] for link in view.get("links", []) if link.get("type") == "approval"), None)
        if approval:
            self.record("approval link on this host", approval.startswith(self.base + "/approve/"), approval)
            r = self.client.get(f"/acp/approvals/{sid}")
            webauthn = r.json().get("webauthn", {}) if r.status_code == 200 else {}
            self.record("passkey challenge for this origin", webauthn.get("origin") == self.base,
                        f"origin {webauthn.get('origin')}, rp_id {webauthn.get('rp_id')}")
        r = self.acp("GET", f"{SESSIONS}/{sid}")
        self.record("read the checkout back", r.status_code == 200 and r.json()["id"] == sid, str(r.status_code))
        r = self.acp("POST", f"{SESSIONS}/{sid}/cancel", {})
        self.record("cancel it (nothing charged)", r.status_code == 200 and r.json()["status"] == "canceled",
                    str(r.status_code))
        return sid

    def mcp(self) -> None:
        if not self.mcp_token:
            self.record("fallback MCP", None, "COUNTERSIGN_MCP_TOKEN not set here")
            return
        auth = {"Authorization": f"Bearer {self.mcp_token}"}

        def rpc(method: str, params: dict[str, Any] | None = None):
            return self.client.post("/acp/mcp", headers=auth, json={"jsonrpc": "2.0", "id": 1, "method": method,
                                                                   "params": params or {}})

        r = rpc("initialize", {"protocolVersion": "2025-06-18", "capabilities": {},
                               "clientInfo": {"name": "smoke", "version": "1"}})
        if not self.record("MCP initialize", r.status_code == 200 and "result" in r.json(), str(r.status_code)):
            return
        tools = [t["name"] for t in rpc("tools/list").json()["result"]["tools"]]
        self.record("MCP tools", "complete_checkout" in tools and "search_products" in tools, ", ".join(tools))
        found = rpc("tools/call", {"name": "search_products", "arguments": {"query": "kettle"}}).json()
        products = found.get("result", {}).get("structuredContent", {}).get("products", [])
        self.record("MCP search finds products", bool(products), f"{len(products)} found")

    def console(self) -> None:
        if not self.lever_token:
            self.record("merchant console", None, "COUNTERSIGN_LEVER_TOKEN not set here")
            return
        r = self.client.get("/api/console/state", headers={"Authorization": f"Bearer {self.lever_token}"})
        self.record("merchant console API", r.status_code == 200, str(r.status_code)
                    + (": not enabled on the host" if r.status_code == 404 else ""))

    # ------------------------------------------------------------ one purchase

    def buy(self, *, timeout: float = 600, every: float = 3) -> None:
        if not self.secret:
            self.record("purchase", None, "needs COUNTERSIGN_ACP_HMAC_SECRET")
            return
        sku = next(p.sku for p in synthetic_catalog() if p.stock > 0)
        view = self.acp("POST", SESSIONS, {"items": [{"id": sku, "quantity": 1}], "currency": "INR"}).json()
        sid = view["id"]
        self.out(f"      checkout {sid}; the shopper's page: {self.base}/approve/{sid}")
        deadline = time.monotonic() + timeout
        state: dict[str, Any] = {}
        while time.monotonic() < deadline:
            state = self.client.get(f"/api/checkout/{sid}").json()
            if state["stage"] == "waiting":
                done = self.acp("POST", f"{SESSIONS}/{sid}/complete", {}).json()
                if done["status"] != "completed":
                    codes = [m.get("code") for m in done.get("messages", [])]
                    self.out(f"      complete answered {done['status']} {codes}; approve again on the page")
            elif state["stage"] in ("paid", "failed", "canceled") and (state["stage"] != "paid" or state["receipt"]):
                break
            elif state["stage"] == "approve":
                self.out("      waiting for the shopper's passkey…")
            elif state["stage"] == "pay":
                self.out(f"      waiting for the card payment at {self.base}/pay/{sid}…")
            self.sleep(every)
        if not self.record("purchase completed and paid", state.get("stage") == "paid" and bool(state.get("receipt")),
                           f"stage {state.get('stage')}"):
            return
        public = self.key.get("public_key") or state["merchant_key"]["public_key"]
        from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

        check = receipts.verify(state["receipt"], Ed25519PublicKey.from_public_bytes(base64.b64decode(public)))
        self.record("receipt verifies against the published key", check.ok, "; ".join(check.findings))

    def run(self, *, buy: bool = False) -> bool:
        self.public()
        if self.health:
            self.platform()
            self.mcp()
            self.console()
            if buy:
                self.buy()
        failed = [c for c in self.checks if c.ok is False]
        skipped = [c for c in self.checks if c.ok is None]
        self.out(f"\n{len(self.checks) - len(failed) - len(skipped)} passed, {len(failed)} failed, {len(skipped)} skipped")
        return not failed


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--base-url", default=os.environ.get("COUNTERSIGN_PUBLIC_URL"))
    parser.add_argument("--buy", action="store_true", help="run one purchase end to end (you approve and pay)")
    args = parser.parse_args()
    if not args.base_url:
        parser.error("--base-url or COUNTERSIGN_PUBLIC_URL is required")
    import httpx

    with httpx.Client(base_url=args.base_url.rstrip("/"), timeout=90, follow_redirects=False) as client:
        smoke = Smoke(client, args.base_url, secret=os.environ.get("COUNTERSIGN_ACP_HMAC_SECRET", ""),
                      mcp_token=os.environ.get("COUNTERSIGN_MCP_TOKEN", ""),
                      lever_token=os.environ.get("COUNTERSIGN_LEVER_TOKEN", ""))
        return 0 if smoke.run(buy=args.buy) else 1


if __name__ == "__main__":
    sys.exit(main())
