"""V2: the merchant's edge, the C7 HTTP API behind a TAP check.

Every request is verified before any route runs. A failed check answers
401 {"refused": "<reason>"} and nothing else happens: no search, no offer, no
charge. Browsing needs an agent-browser-auth signature; paying and looking up
an order need agent-payer-auth.

The authority checked is the merchant's own host, passed in, never the Host
header the caller sends: a signature made for another merchant must not verify
here just because the caller says it is somewhere else.
"""

from __future__ import annotations

import threading
from datetime import datetime, timezone
from typing import Any, Callable

from fastapi import FastAPI, Query, Request
from fastapi.responses import HTMLResponse, JSONResponse, Response
from pydantic import BaseModel, Field
from starlette.concurrency import run_in_threadpool

from .. import receipt as receipts
from .. import tap, wire
from ..catalog_gmc import lookup
from ..checkout import MemoryReceiptStore, ReceiptStore, countersign
from ..decide import Decision, Outcome
from ..merchant import MerchantGateway, Product
from ..terms import Approval, utc
from . import acp as acp_api
from . import pages

# The tag each route needs. Anything not listed still needs a valid signature.
ROUTE_TAGS = (
    ("GET", "/products", tap.BROWSE),
    ("POST", "/offers", tap.BROWSE),
    ("POST", "/checkout", tap.PAY),
    ("GET", "/orders/", tap.PAY),
)


def required_tag(method: str, path: str) -> str | None:
    for m, prefix, tag in ROUTE_TAGS:
        if method == m and (path == prefix or (prefix.endswith("/") and path.startswith(prefix))):
            return tag
    return None


# The largest body the edge reads. A payment signs its body, so the body is read
# before the TAP check; without a cap, any caller, signed or not, could make the
# edge hold an arbitrarily large body in memory. A real approval is a few KiB.
MAX_BODY = 64 * 1024


def body_refusal(content_length: str | None) -> tuple[int, str] | None:
    """Why a request body is refused before a byte of it is read, or None."""
    if content_length is None:  # chunked: the cap could not be checked up front
        return 411, "a request body must declare its Content-Length"
    if not content_length.isdigit() or int(content_length) > MAX_BODY:
        return 413, f"the request body is over {MAX_BODY} bytes"
    return None


def signed_path(request: Request) -> str:
    """The path as sent, query string included: what Visa's proxy signs (req.url)."""
    raw = request.scope.get("raw_path") or request.url.path.encode()
    query = request.scope.get("query_string") or b""
    return raw.decode("latin-1") + (f"?{query.decode('latin-1')}" if query else "")


class OfferRequest(BaseModel):
    cart: dict[str, int] = Field(min_length=1)
    shopper_currency: str = Field(pattern=r"^[A-Z]{3}$")


class CheckoutRequest(BaseModel):
    approval: dict[str, Any]
    idempotency_key: str = Field(pattern=r"^[A-Za-z0-9._:-]{1,128}$")


def read_approval(data: dict[str, Any]) -> Approval:
    """The approval as sent, or ValueError: anything unreadable is refused, never guessed."""
    try:
        approval = wire.approval_from_json(data)
        utc(approval.approved_at), utc(approval.expires_at)
    except (KeyError, TypeError, ValueError, ArithmeticError) as err:
        raise ValueError(f"the approval could not be read ({type(err).__name__})") from None
    if not approval.terms.items:
        raise ValueError("the approval has no items")
    if not approval.ceiling.is_finite():
        raise ValueError("the approval's ceiling is not a number")
    return approval


def checkout_json(decision: Decision, order=None, receipt=None) -> dict[str, Any]:
    return {"decision": wire.decision_to_json(decision),
            "order": wire.order_to_json(order) if order else None,
            "receipt": receipt}


def create_app(
    merchant: MerchantGateway,
    directory: tap.KeyDirectory,
    *,
    authority: str,
    nonces: tap.NonceCache | None = None,
    signer: receipts.MerchantSigner | None = None,
    store: ReceiptStore | None = None,
    clock: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
    products: Callable[[str], Product | None] | None = None,
    acp: acp_api.AcpConfig | None = None,
) -> FastAPI:
    app = FastAPI(title="Countersign merchant edge", docs_url=None, redoc_url=None, openapi_url=None)
    nonce_cache = nonces if nonces is not None else tap.MemoryNonceCache()
    receipt_store = store if store is not None else MemoryReceiptStore()
    find_product = products if products is not None else lookup(merchant)
    # Requests run in parallel threads, so "check for an order, then pay" must not
    # interleave for one key: a retry that lands mid-charge waits, then finds the order.
    key_locks: dict[str, threading.Lock] = {}
    key_locks_guard = threading.Lock()

    def lock_for(idempotency_key: str) -> threading.Lock:
        with key_locks_guard:
            return key_locks.setdefault(idempotency_key, threading.Lock())

    # Which agent placed the order under each key. An order and its receipt (which
    # names the shopper) are only ever shown to the agent that placed it.
    placed_by: dict[str, tuple[str, str]] = {}

    def who(agent: tap.AgentIdentity) -> tuple[str, str]:
        return agent.signature_agent, agent.key_id

    edge_log: list[dict[str, Any]] = []
    app.state.edge_log = edge_log

    @app.middleware("http")
    async def tap_check(request: Request, call_next):
        # The shop window: the product pages and images that the catalogue rows link
        # to. Anyone may look; nothing there can offer, charge or show an order.
        if pages.is_public(request.method, request.url.path):
            return await call_next(request)
        # The checkout API Airwallex calls (C10): a platform, not an agent, so it is
        # authenticated by the shared HMAC secret in its own routes, not by TAP.
        # The body cap still applies. Without an ACP config these paths stay behind TAP.
        if acp is not None and acp_api.is_acp(request.url.path):
            if request.method in ("POST", "PUT", "PATCH"):
                refusal = body_refusal(request.headers.get("content-length"))
                if refusal is not None:
                    status, reason = refusal
                    return JSONResponse({"refused": reason}, status_code=status)
            return await call_next(request)
        path = signed_path(request)
        tag = required_tag(request.method, request.url.path)
        # A request with a body is checked against the body actually received, and a
        # payment must sign its body: otherwise the approval it carries could be swapped
        # under a valid signature.
        body = None
        if request.method in ("POST", "PUT", "PATCH"):
            refusal = body_refusal(request.headers.get("content-length"))
            if refusal is not None:
                status, reason = refusal
                edge_log.append({"path": path, "tag": tag, "ok": False, "detail": reason})
                del edge_log[:-500]
                return JSONResponse({"refused": reason}, status_code=status)
            body = await request.body()
        # Off the event loop: a key lookup can wait on a remote registry, and one
        # slow agent must not hold up every other request at the edge.
        result = await run_in_threadpool(tap.verify, dict(request.headers), authority=authority, path=path,
                                         directory=directory, nonces=nonce_cache, require_tag=tag,
                                         body=body, require_body=tag == tap.PAY and body is not None)
        ok = isinstance(result, tap.AgentIdentity)
        edge_log.append({"path": path, "tag": tag, "ok": ok, "detail": result.key_id if ok else result.reason})
        del edge_log[:-500]
        if not ok:
            return JSONResponse({"refused": result.reason}, status_code=401)
        request.state.agent = result
        return await call_next(request)

    if acp is not None:
        app.state.acp = acp_api.AcpApi(merchant, acp, clock=clock, signer=signer, store=receipt_store)
        app.include_router(acp_api.router(app.state.acp))

    @app.get("/p/{sku}")
    def product_page(sku: str):
        product = find_product(sku)
        if product is None:
            return HTMLResponse("<!doctype html><title>Not found</title><p>No such product.</p>", status_code=404)
        return HTMLResponse(pages.product_page(product, merchant.currency),
                            headers={"Cache-Control": "public, max-age=60"})

    @app.get("/img/{sku}.svg")
    def product_image(sku: str):
        product = find_product(sku)
        if product is None:
            return Response(status_code=404)
        return Response(pages.product_svg(product), media_type="image/svg+xml",
                        headers={"Cache-Control": "public, max-age=3600"})

    @app.get("/products")
    def products(q: str = "", limit: int = Query(5, ge=1, le=50)) -> list[dict[str, Any]]:
        return [wire.product(p) for p in merchant.search(q, limit)]

    @app.post("/offers")
    def offers(body: OfferRequest):
        if any(qty < 1 for qty in body.cart.values()):
            return JSONResponse({"error": "every quantity must be at least 1"}, status_code=400)
        try:
            terms = merchant.offer(body.cart, body.shopper_currency, clock())
        except KeyError as err:
            return JSONResponse({"error": f"no product with sku {err.args[0]}"}, status_code=404)
        except LookupError as err:
            return JSONResponse({"error": str(err)}, status_code=409)
        return wire.terms_to_json(terms)

    @app.post("/checkout")
    def checkout(body: CheckoutRequest, request: Request) -> dict[str, Any]:
        # A broken approval is a refusal in the C7 shape, so the agent can say why.
        try:
            approval = read_approval(body.approval)
        except ValueError as err:
            return checkout_json(Decision(Outcome.REFUSE, str(err)))
        agent = request.state.agent
        with lock_for(body.idempotency_key):
            if placed_by.get(body.idempotency_key, who(agent)) != who(agent):
                return checkout_json(Decision(Outcome.REFUSE, "this idempotency key is already in use"))
            result = countersign(merchant, approval, body.idempotency_key, clock(),
                                 agent=agent, signer=signer, store=receipt_store)
            if result.order is not None:
                placed_by.setdefault(body.idempotency_key, who(agent))
        return checkout_json(result.decision, result.order, result.receipt)

    @app.get("/orders/{idempotency_key}")
    def order(idempotency_key: str, request: Request):
        found = merchant.find_order(idempotency_key)
        mine = who(request.state.agent)
        if found is None or placed_by.get(idempotency_key, mine) != mine:
            return JSONResponse({"error": "no order under this idempotency key"}, status_code=404)
        return wire.order_to_json(found)

    return app
