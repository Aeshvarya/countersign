"""What the one public host serves for its house merchant, besides the merchant's own edge:

    /healthz                               liveness, and what is switched on (for the uptime monitor and smoke.py)
    /.well-known/countersign-merchant-key  the key that verifies the house merchant's receipts
    /approve/<id>, /pay/<id>               the shopper's page: passkey approval, card payment, receipt
    /verify                                anyone checks a receipt in their own browser
    /receipts/<checkout id>                the signed receipt for a payment Airwallex's MCP made (W3); the
                                           id (chk_ and 96 random bits) is the capability, as a session id is
    /console                               the merchant console (its API needs COUNTERSIGN_LEVER_TOKEN)
    /static/<script>                       those pages' scripts
    /api/checkout/...                      the shopper page's API (web.shopper)
    /api/console/..., /api/levers/...      the console's API and the levers (web.console), only with a token

The shopper page's address carries the session id, which is the capability
(edge.acp). So: only the origin is ever sent as a referrer, nothing loads from
other sites except Airwallex's Drop-in, and no site can frame it (a framed page
could be clicked through by a page laid on top of it).
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from fastapi import APIRouter, HTTPException
from fastapi.responses import FileResponse, JSONResponse

from . import console, shopper
from .host import KEY_PATH

STATIC = Path(__file__).parent / "static"
AIRWALLEX = "https://*.airwallex.com"
SHOPPER_PAGE_HEADERS = {
    "Content-Security-Policy": f"default-src 'none'; script-src 'self' {AIRWALLEX}; style-src 'unsafe-inline'; "
                               f"connect-src 'self' {AIRWALLEX}; frame-src {AIRWALLEX}; img-src 'self' data: {AIRWALLEX}; "
                               "base-uri 'none'; form-action 'none'; frame-ancestors 'none'",
    "Referrer-Policy": "strict-origin",
    "X-Frame-Options": "DENY",
    "X-Content-Type-Options": "nosniff",
    "Cache-Control": "no-store",
}
PLAIN_PAGE_HEADERS = {  # /verify and /console: this site only
    **SHOPPER_PAGE_HEADERS,
    "Content-Security-Policy": "default-src 'none'; script-src 'self'; style-src 'unsafe-inline'; connect-src 'self'; "
                               "img-src 'self' data:; base-uri 'none'; form-action 'none'; frame-ancestors 'none'",
    "Referrer-Policy": "no-referrer",
}
SCRIPTS = ("shopper.js", "receipt.js", "verify.js", "console.js")


def router(house: Any) -> APIRouter:
    r = APIRouter()

    @r.get("/healthz")
    def healthz() -> dict[str, Any]:
        api = getattr(house.edge.state, "acp", None)
        return {"status": "ok", "acp": house.acp, "authority": house.authority,
                "shopper_passkey": bool(api and api.config.require_shopper_signature),
                "card_payments": hasattr(house.merchant, "confirm"),
                "fallback_mcp": bool(house.mcp_token), "console": bool(house.lever_token),
                "oauth": getattr(house, "oauth", None) is not None, "keep_awake": bool(getattr(house, "keep_awake", False)),
                "jwks_fetches": list(house.oauth.jwks_fetches) if getattr(house, "oauth", None) else [],
                "mcp_payments": getattr(house, "settler", None) is not None}

    @r.get(KEY_PATH)
    def merchant_key() -> dict[str, str]:
        """The public key that verifies every receipt the house merchant signs."""
        return house.public_key()

    @r.get("/approve/{session_id}")
    @r.get("/pay/{session_id}")
    def shopper_page(session_id: str) -> FileResponse:
        """The link a waiting checkout carries (`links`, type approval), and the one the agent
        gives the shopper to pay. One page: approve, wait for the agent, pay, keep the receipt."""
        return FileResponse(STATIC / "shopper.html", headers=SHOPPER_PAGE_HEADERS)

    @r.get("/verify")
    def verify_page() -> FileResponse:
        """Anyone with a receipt (the shopper, their bank, a dispute team) checks it in their own browser."""
        return FileResponse(STATIC / "verify.html", headers=PLAIN_PAGE_HEADERS)

    @r.get("/receipt/{checkout_id}")
    def order_page(checkout_id: str) -> FileResponse:
        """The permalink Airwallex hands the agent for a paid order: /verify, which fetches this order's
        signed receipt from /receipts/<checkout id>, waits for it while the payment settles, and checks it."""
        return FileResponse(STATIC / "verify.html", headers=PLAIN_PAGE_HEADERS)

    @r.get("/receipts/{checkout_id}")
    def mcp_receipt(checkout_id: str) -> Any:
        """The receipt the merchant signed when a payment Airwallex's MCP made matched its quote (W3)."""
        found = house.store.get(checkout_id) if checkout_id.startswith("chk_") else None
        if found is None:
            raise HTTPException(404, "no signed receipt for this checkout")
        return JSONResponse(found, headers={"Cache-Control": "no-store", "Referrer-Policy": "no-referrer"})

    @r.get("/console")
    def console_page() -> FileResponse:
        """A15: open it as /console#t=<COUNTERSIGN_LEVER_TOKEN>; the fragment never leaves the browser."""
        return FileResponse(STATIC / "console.html", headers=PLAIN_PAGE_HEADERS)

    @r.get("/static/{name}")
    def static_script(name: str) -> FileResponse:
        if name not in SCRIPTS:
            raise HTTPException(404, "not found")
        return FileResponse(STATIC / name, media_type="text/javascript",
                            headers={"X-Content-Type-Options": "nosniff", "Cache-Control": "no-cache"})

    r.include_router(shopper.router(house))
    if house.lever_token:
        r.include_router(console.router(house))
    return r
