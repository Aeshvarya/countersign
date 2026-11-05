"""Screenshot every screen, as a first-time viewer sees it, so a UI change is reviewed as pictures.

    pip install playwright        (once; it drives the Chrome already on this machine)
    python scripts/screens.py                 every screen, desktop and phone, light and dark
    python scripts/screens.py --only approve  one screen
    python scripts/screens.py --out runs/local/screens

It starts the whole service on localhost, offline: the scripted agent, the
synthetic shop, the /acp door with a throwaway HMAC secret, passkeys on (U1)
with Chrome's virtual authenticator standing in for Face ID. It then walks
one checkout through every stage the shopper and the merchant see:

    home       /                     the demo's first screen
    shop       /p/<sku>              the catalogue page Airwallex links to
    approve    /approve/<id>         the exact terms, waiting for the passkey
    waiting    the same, signed      approved; the agent has not completed yet
    reask      the same, re-asked    the price rose after the yes; nothing charged
    receipt    the same, paid        the signed receipt, checked in the browser
    verify     /verify               the receipt checker with that receipt
    console    /console              the merchant's live feed after all of it

Each screen is saved as <name>-<desktop|phone>-<light|dark>.png. Nothing
leaves this machine; every secret is made for the run and thrown away.
"""

from __future__ import annotations

import argparse
import base64
import json
import os
import secrets
import socket
import sys
import threading
import time
from pathlib import Path

SIZES = {"desktop": (1440, 900), "phone": (390, 844)}
THEMES = ("light", "dark")
SCREENS = ("home", "shop", "approve", "waiting", "reask", "receipt", "verify", "console")
SESSIONS = "/acp/checkout_sessions"


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def serve(port: int, env: dict[str, str]):
    """The service in this process, on localhost, with the run's own settings."""
    os.environ.update(env)
    import uvicorn

    from countersign.web.app import app

    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning"))
    threading.Thread(target=server.run, daemon=True).start()
    deadline = time.time() + 15
    while not server.started:
        if time.time() > deadline:
            raise RuntimeError("the service did not start")
        time.sleep(0.05)
    return server


class Door:
    """Calls /acp as Airwallex would (HMAC-signed), and the levers as the merchant would."""

    def __init__(self, base: str, secret: str, lever_token: str):
        import httpx

        from countersign.edge import hmac_auth

        self.http, self.sign = httpx.Client(base_url=base, timeout=10), hmac_auth.sign
        self.secret, self.lever = secret.encode(), {"Authorization": f"Bearer {lever_token}"}

    def acp(self, path: str, body: dict | None = None) -> dict:
        raw = json.dumps(body or {}).encode()
        headers = {"content-type": "application/json", "Idempotency-Key": secrets.token_hex(8),
                   **self.sign(raw, self.secret)}
        r = self.http.post(path, content=raw, headers=headers)
        r.raise_for_status()
        return r.json()

    def checkout(self, sku: str) -> str:
        return self.acp(SESSIONS, {"items": [{"id": sku, "quantity": 1}], "currency": "EUR"})["id"]

    def raise_price(self, sku: str, percent: int) -> None:
        self.http.post("/api/levers/price", json={"sku": sku, "percent": percent}, headers=self.lever).raise_for_status()

    def reset(self) -> None:
        self.http.post("/api/levers/reset", json={}, headers=self.lever).raise_for_status()


def wait_for(page, selector: str, timeout: float = 8000) -> None:
    page.wait_for_selector(selector, state="visible", timeout=timeout)


def passkey(page) -> None:
    """Chrome's virtual authenticator: a platform passkey that always verifies the user."""
    cdp = page.context.new_cdp_session(page)
    cdp.send("WebAuthn.enable")
    cdp.send("WebAuthn.addVirtualAuthenticator", {"options": {
        "protocol": "ctap2", "transport": "internal", "hasResidentKey": True,
        "hasUserVerification": True, "isUserVerified": True, "automaticPresenceSimulation": True}})


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--out", default="runs/local/screens", help="where the pictures go (default %(default)s)")
    parser.add_argument("--only", choices=SCREENS, action="append", help="just these screens (repeatable)")
    parser.add_argument("--size", choices=tuple(SIZES), action="append", help="just these sizes")
    parser.add_argument("--theme", choices=THEMES, action="append", help="just these themes")
    args = parser.parse_args(argv)
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        print("needs Playwright: pip install playwright", file=sys.stderr)
        return 2

    port = free_port()
    base = f"http://localhost:{port}"
    secret, lever_token = secrets.token_hex(32), secrets.token_hex(32)
    serve(port, {
        "COUNTERSIGN_MODEL": "scripted",
        "COUNTERSIGN_PUBLIC_URL": base,
        "COUNTERSIGN_ACP_HMAC_SECRET": secret,
        "COUNTERSIGN_LEVER_TOKEN": lever_token,
        "COUNTERSIGN_REQUIRE_SHOPPER_SIGNATURE": "1",
        "COUNTERSIGN_MERCHANT_SIGNING_KEY": base64.b64encode(secrets.token_bytes(32)).decode(),
    })
    door = Door(base, secret, lever_token)
    from countersign.merchant import FakeMerchant
    from countersign.fx import FakeFx

    sku = FakeMerchant(FakeFx("84.10")).search("kettle")[0].sku
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    wanted = set(args.only or SCREENS)
    saved: list[Path] = []

    with sync_playwright() as pw:
        browser = pw.chromium.launch(channel="chrome")
        for size in args.size or SIZES:
            for theme in args.theme or THEMES:
                width, height = SIZES[size]
                context = browser.new_context(viewport={"width": width, "height": height}, color_scheme=theme,
                                              is_mobile=size == "phone", has_touch=size == "phone",
                                              device_scale_factor=2 if size == "phone" else 1)
                page = context.new_page()
                passkey(page)

                def shot(name: str) -> None:
                    if name in wanted:
                        path = out / f"{name}-{size}-{theme}.png"
                        page.wait_for_timeout(400)  # let state changes and the feed's new rows settle
                        page.screenshot(path=str(path), full_page=True)
                        saved.append(path)

                door.reset()
                page.goto(f"{base}/")
                shot("home")
                page.goto(f"{base}/p/{sku}")
                shot("shop")

                sid = door.checkout(sku)
                page.goto(f"{base}/approve/{sid}")
                wait_for(page, "#approve:not([disabled])")
                shot("approve")
                page.click("#approve")
                wait_for(page, "#wait-step")
                shot("waiting")

                door.raise_price(sku, 12)
                door.acp(f"{SESSIONS}/{sid}/complete")  # held: the terms the passkey signed are gone
                page.reload()
                wait_for(page, "#approve:not([disabled])")
                shot("reask")
                page.click("#approve")
                wait_for(page, "#wait-step")
                done = door.acp(f"{SESSIONS}/{sid}/complete")
                page.reload()
                wait_for(page, "#receipt-step")
                page.wait_for_selector("#checks li", timeout=8000)
                shot("receipt")

                receipt = (done.get("countersign") or {}).get("receipt")
                page.goto(f"{base}/verify")
                if receipt is not None:
                    page.fill("#receipt", json.dumps(receipt, indent=2))
                    wait_for(page, "#key")
                    page.wait_for_function("document.querySelector('#key').value.length > 0", timeout=8000)
                    page.click("#check")
                    page.wait_for_timeout(300)
                shot("verify")

                page.goto(f"{base}/console#t={lever_token}")
                page.wait_for_selector("#feed li:not(.empty)", timeout=8000)
                shot("console")
                context.close()
        browser.close()

    for path in saved:
        print(path)
    print(f"{len(saved)} screens in {out}/")
    return 0


if __name__ == "__main__":
    sys.exit(main())
