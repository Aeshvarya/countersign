"""A7: the one public host. Everything Airwallex, an agent or a shopper reaches is here.

The hosted demo builds a private merchant per visitor, inside the page. The real
merchant, the one Airwallex onboards, needs a fixed address instead:

    /acp/checkout_sessions...       create_checkout, called by Airwallex (HMAC, C10)
    /p/<sku>, /img/<sku>.svg        the shop window every catalogue row links to (C11)
    /products /offers /checkout     the direct door, for agents that sign with TAP (C7)
    /orders/<key>
    /.well-known/countersign-merchant-key   the key that verifies this merchant's receipts
    /webhooks/airwallex             payment events; settle a card payment and sign its receipt (C9)
    /acp/mcp                        our fallback merchant MCP, only if COUNTERSIGN_MCP_TOKEN is set
    everything else                 the demo page and its API

So one service answers all of it: those paths go to one long-lived merchant
behind the real edge (the house), and the rest to the demo. The house keeps its
state in memory; a restart forgets open checkout sessions, never a charge,
because nothing is charged without a fresh countersign.

Configuration, all optional:

    COUNTERSIGN_PUBLIC_URL             https URL of this service; its host is the TAP authority
    COUNTERSIGN_ACP_HMAC_SECRET        mounts /acp (see edge.acp.AcpConfig.from_env for the rest)
    COUNTERSIGN_MERCHANT_SIGNING_KEY   base64 of a 32-byte Ed25519 seed, so receipts outlive a restart
    COUNTERSIGN_MERCHANT_KEY_ID        default merchant-key-1
    COUNTERSIGN_AGENT_REGISTRY         URL of a Visa-shaped agent registry; without it the TAP door refuses everyone
    COUNTERSIGN_MERCHANT               fake (default, pays instantly) or airwallex (real intents, V5′)
    AIRWALLEX_CLIENT_ID, AIRWALLEX_API_KEY   real rate locks, and the airwallex merchant
    AIRWALLEX_WEBHOOK_SECRET           verifies /webhooks/airwallex; without it every event is refused
    COUNTERSIGN_MCP_TOKEN              bearer token for /acp/mcp (at least 32 characters); needs /acp
    COUNTERSIGN_LEVER_TOKEN            bearer token for the merchant console and its levers (32+ characters)
    COUNTERSIGN_SYNC_CATALOG           1: a lever also upserts the product to Airwallex's catalogue
"""

from __future__ import annotations

import base64
import binascii
import json
import logging
from dataclasses import dataclass, field
from typing import Any, Callable, Mapping
from urllib.parse import urlparse

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from starlette.concurrency import run_in_threadpool

from .. import receipt as receipts
from .. import tap
from ..airwallex import Airwallex, AirwallexError
from ..airwallex_merchant import AirwallexMerchant, intent_of
from ..checkout import MemoryReceiptStore, release
from ..edge import acp as acp_api
from ..edge import create_app as create_edge
from ..edge import hmac_auth, pages
from ..fx import AirwallexFx, FakeFx
from ..merchant import FakeMerchant, MerchantGateway
from .console import Feed, FeedMiddleware
from .merchant_mcp import MCP_PATH, mcp_app

log = logging.getLogger("countersign.host")

DEFAULT_AUTHORITY = "shop.countersign.dev"
KEY_PATH = "/.well-known/countersign-merchant-key"
WEBHOOK_PATH = "/webhooks/airwallex"
# The direct door's routes (C7). Exact paths, so the demo's own routes never reach the edge.
TAP_PATHS = ("/products", "/offers", "/checkout")


def authority_of(public_url: str | None) -> str:
    if not public_url:
        return DEFAULT_AUTHORITY
    parsed = urlparse(public_url)
    if parsed.scheme != "https" or not parsed.hostname:
        raise ValueError("COUNTERSIGN_PUBLIC_URL must be an https URL")
    return parsed.netloc


def signing_key_from(environ: Mapping[str, str]) -> Ed25519PrivateKey:
    seed = environ.get("COUNTERSIGN_MERCHANT_SIGNING_KEY", "").strip()
    if not seed:
        log.warning("no COUNTERSIGN_MERCHANT_SIGNING_KEY: receipts signed now stop verifying after a restart")
        return Ed25519PrivateKey.generate()
    try:
        raw = base64.b64decode(seed, validate=True)
    except binascii.Error:
        raise ValueError("COUNTERSIGN_MERCHANT_SIGNING_KEY is not base64") from None
    if len(raw) != 32:
        raise ValueError("COUNTERSIGN_MERCHANT_SIGNING_KEY must decode to 32 bytes")
    return Ed25519PrivateKey.from_private_bytes(raw)


class TableFx:
    """Offline rate locks per shopper currency, for the house before sandbox keys exist."""

    RATES = {"INR": "84.10", "EUR": "0.92", "GBP": "0.79", "SGD": "1.30", "AUD": "1.52"}

    def __init__(self) -> None:
        self.fakes = {ccy: FakeFx(rate) for ccy, rate in self.RATES.items()}

    def lock(self, merchant_currency: str, shopper_currency: str, now):
        if shopper_currency not in self.fakes:
            raise LookupError(f"{shopper_currency} is not a shopper currency this merchant prices in")
        return self.fakes[shopper_currency].lock(merchant_currency, shopper_currency, now)


def airwallex_from_env(environ: Mapping[str, str]) -> Airwallex | None:
    if environ.get("AIRWALLEX_CLIENT_ID") and environ.get("AIRWALLEX_API_KEY"):
        return Airwallex(environ["AIRWALLEX_CLIENT_ID"], environ["AIRWALLEX_API_KEY"],
                         environ.get("AIRWALLEX_BASE_URL", "https://api.sandbox.airwallex.com"))
    return None


def fx_from_env(environ: Mapping[str, str]):
    """Real Airwallex rate locks once sandbox keys are set; offline ones before."""
    client = airwallex_from_env(environ)
    return AirwallexFx(client) if client is not None else TableFx()


def merchant_from_env(environ: Mapping[str, str]) -> MerchantGateway:
    """COUNTERSIGN_MERCHANT=airwallex takes real card payments (V5′); the default pays instantly, offline."""
    kind = environ.get("COUNTERSIGN_MERCHANT", "fake").strip().lower()
    if kind == "fake":
        return FakeMerchant(fx_from_env(environ))
    if kind == "airwallex":
        client = airwallex_from_env(environ)
        if client is None:
            raise ValueError("COUNTERSIGN_MERCHANT=airwallex needs AIRWALLEX_CLIENT_ID and AIRWALLEX_API_KEY")
        return AirwallexMerchant(client)
    raise ValueError("COUNTERSIGN_MERCHANT must be fake or airwallex")


@dataclass
class House:
    """The merchant Airwallex onboards, behind the real edge."""

    merchant: MerchantGateway
    edge: Any  # the edge's FastAPI app
    signer: receipts.MerchantSigner
    authority: str
    acp: bool
    store: MemoryReceiptStore
    webhook_secret: bytes = b""
    mcp_token: bytes = b""  # mounts the fallback merchant MCP when set
    public_url: str | None = None
    feed: Feed = field(default_factory=Feed)  # what the merchant console shows (A15)
    lever_token: bytes = b""  # mounts the console's API and the levers (V6) when set
    airwallex: Airwallex | None = None  # for the levers' catalogue sync
    sync_catalog: bool = False

    def public_key(self) -> dict[str, str]:
        raw = self.signer.key.public_key().public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)
        return {"key_id": self.signer.key_id, "alg": "ed25519", "public_key": base64.b64encode(raw).decode()}

    def owns(self, method: str, path: str) -> bool:
        return (acp_api.is_acp(path) or pages.is_public(method, path)
                or path in TAP_PATHS or path.startswith("/orders/"))


def house_from_env(environ: Mapping[str, str], *, merchant: MerchantGateway | None = None,
                   directory: tap.KeyDirectory | None = None, clock: Callable | None = None) -> House:
    """Build the house from configuration. A bad value stops the service from starting, loudly."""
    authority = authority_of(environ.get("COUNTERSIGN_PUBLIC_URL"))
    signer = receipts.MerchantSigner(signing_key_from(environ),
                                     environ.get("COUNTERSIGN_MERCHANT_KEY_ID", "merchant-key-1"))
    if directory is None:
        registry = environ.get("COUNTERSIGN_AGENT_REGISTRY")
        directory = tap.RegistryDirectory(registry) if registry else tap.StaticDirectory({})
    # Either way the catalogue is the one uploaded to Airwallex.
    merchant = merchant if merchant is not None else merchant_from_env(environ)
    acp = acp_api.AcpConfig.from_env(environ)
    store = MemoryReceiptStore()  # shared with the webhook, which signs held receipts into it
    extra = {"clock": clock} if clock is not None else {}
    mcp_token = environ.get("COUNTERSIGN_MCP_TOKEN", "").strip()
    if mcp_token and len(mcp_token) < 32:
        raise ValueError("COUNTERSIGN_MCP_TOKEN must be at least 32 characters")
    if mcp_token and acp is None:
        raise ValueError("COUNTERSIGN_MCP_TOKEN needs /acp: set COUNTERSIGN_ACP_HMAC_SECRET too")
    lever_token = environ.get("COUNTERSIGN_LEVER_TOKEN", "").strip()
    if lever_token and len(lever_token) < 32:
        raise ValueError("COUNTERSIGN_LEVER_TOKEN must be at least 32 characters")
    tokens = [t for t in (mcp_token, lever_token) if t]
    if len(set(tokens)) < len(tokens) or (acp is not None and any(t.encode() == acp.secret for t in tokens)):
        raise ValueError("COUNTERSIGN_MCP_TOKEN, COUNTERSIGN_LEVER_TOKEN and the HMAC secret must all differ")
    edge = create_edge(merchant, directory, authority=authority, signer=signer, acp=acp, store=store, **extra)
    feed = Feed()
    edge.add_middleware(FeedMiddleware, feed=feed)  # sees Airwallex's calls and our MCP's alike
    sync = environ.get("COUNTERSIGN_SYNC_CATALOG", "").strip().lower() in ("1", "true", "yes")
    return House(merchant, edge, signer, authority, acp is not None, store,
                 environ.get("AIRWALLEX_WEBHOOK_SECRET", "").encode(), mcp_token.encode(),
                 environ.get("COUNTERSIGN_PUBLIC_URL") or None, feed, lever_token.encode(),
                 airwallex_from_env(environ), sync)


def webhook_app(house: House) -> FastAPI:
    """POST /webhooks/airwallex: settle a card payment from Airwallex's own record of it.

    The event is only a nudge. Its signature is checked (x-timestamp and
    x-signature, HMAC-SHA256 under the webhook secret), and then the merchant
    fetches the intent itself (AirwallexMerchant.confirm) before anything is
    marked paid or refunded. A non-2xx answer makes Airwallex retry.
    """
    hooks = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)

    @hooks.post(WEBHOOK_PATH)
    async def airwallex_event(request: Request):
        raw = await request.body()
        reason = hmac_auth.verify(dict(request.headers), raw, house.webhook_secret, scheme="airwallex")
        if reason is not None:
            return JSONResponse({"refused": reason}, status_code=401)
        try:
            event = json.loads(raw)
        except ValueError:
            return JSONResponse({"error": "the body is not JSON"}, status_code=400)
        intent_id = intent_of(event) if isinstance(event, dict) else None
        if intent_id is None or not isinstance(house.merchant, AirwallexMerchant):
            return {"ignored": True}
        try:
            order = await run_in_threadpool(house.merchant.confirm, intent_id)
        except AirwallexError as err:
            log.warning("webhook for %s not settled yet: %s", intent_id, err)
            return JSONResponse({"error": "Airwallex could not be reached; retry"}, status_code=503)
        if order is None:
            return {"ignored": True}
        receipt = release(order, house.signer, house.store)
        record_payment(house, order, receipt, "Airwallex webhook")
        return {"order_number": order.order_number, "status": order.status, "receipt": receipt is not None}

    return hooks


def record_payment(house: House, order: Any, receipt: dict[str, Any] | None, source: str) -> None:
    """One console line when a card payment settles, from the webhook or the shopper page's check."""
    if order.status == "pending":
        return
    if order.status == "paid":
        house.feed.add("payment", "go", f"Payment confirmed with Airwallex: {order.order_number}",
                       f"{order.amount} {order.currency}" + (" · receipt signed" if receipt else "") + f" · {source}",
                       order=order.order_number)
    else:
        reasons = "; ".join(getattr(house.merchant, "problems", {}).get(order.idempotency_key, ()))
        house.feed.add("payment", "no", f"Payment {order.status}: {order.order_number}", reasons or source,
                       order=order.order_number)


class Host:
    """ASGI: the house's paths to the house edge, the rest to the demo."""

    def __init__(self, demo: Any, house: House) -> None:
        self.demo = demo
        self.house = house
        self.hooks = webhook_app(house)
        self.mcp = mcp_app(house, house.mcp_token, house.public_url) if house.mcp_token else None

    async def __call__(self, scope, receive, send) -> None:
        if scope["type"] == "http" and scope["path"] == WEBHOOK_PATH:
            await self.hooks(scope, receive, send)
        elif scope["type"] == "http" and scope["path"] == MCP_PATH and self.mcp is not None:
            await self.mcp(scope, receive, send)
        elif scope["type"] == "http" and self.house.owns(scope["method"], scope["path"]):
            await self.house.edge(scope, receive, send)
        else:
            await self.demo(scope, receive, send)
