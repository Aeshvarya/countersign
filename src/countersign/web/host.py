"""A7: the one public host. Everything Airwallex, an agent or a shopper reaches is here.

The hosted demo builds a private merchant per visitor, inside the page. The real
merchant, the one Airwallex onboards, needs a fixed address instead:

    /acp/checkout_sessions...       create_checkout, called by Airwallex (HMAC, C10)
    /p/<sku>, /img/<sku>.svg        the shop window every catalogue row links to (C11)
    /products /offers /checkout     the direct door, for agents that sign with TAP (C7)
    /orders/<key>
    /.well-known/countersign-merchant-key   the key that verifies this merchant's receipts
    /webhooks/airwallex             payment events; settle a card payment and sign its receipt (C9),
                                    including the ones Airwallex's MCP makes for our create_checkout (W3)
    /acp/mcp                        our fallback merchant MCP, only if COUNTERSIGN_MCP_TOKEN is set
    /.well-known/openid-configuration, /.well-known/jwks.json, /oauth/...
                                    the OAuth server Claude signs the shopper in through (C14, web/oauth.py)
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
    COUNTERSIGN_OAUTH_SIGNING_KEY      the OAuth server's ES256 key (web/oauth.py); it is served once a public URL is set
    COUNTERSIGN_KEEP_AWAKE             1: this process GETs its own public /healthz every 5 minutes, so a free
                                       host that sleeps after about 15 idle minutes never does (keep_awake)
"""

from __future__ import annotations

import base64
import binascii
import json
import logging
import threading
import urllib.error
import urllib.request
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
from ..airwallex import UNREACHABLE, Airwallex, AirwallexError
from ..airwallex_merchant import AirwallexMerchant, intent_of
from ..checkout import MemoryReceiptStore, release
from ..edge import acp as acp_api
from ..edge import create_app as create_edge
from ..edge import hmac_auth, pages
from ..fx import AirwallexFx, FakeFx
from ..merchant import FakeMerchant, MerchantGateway
from ..settle import Settlement, Settler
from .console import Feed, FeedMiddleware
from .merchant_mcp import MCP_PATH, mcp_app
from .oauth import PATHS as OAUTH_PATHS
from .oauth import OAuthServer, oauth_app
from .oauth import server_from_env as oauth_from_env

log = logging.getLogger("countersign.host")

DEFAULT_AUTHORITY = "shop.countersign.dev"
KEY_PATH = "/.well-known/countersign-merchant-key"
WEBHOOK_PATH = "/webhooks/airwallex"
# The direct door's routes (C7). Exact paths, so the demo's own routes never reach the edge.
TAP_PATHS = ("/products", "/offers", "/checkout")
KEEP_AWAKE_EVERY = 300.0  # seconds; Render's free plan sleeps after about 15 idle minutes


def authority_of(public_url: str | None) -> str:
    if not public_url:
        return DEFAULT_AUTHORITY
    parsed = urlparse(public_url)
    # Plain http only for this machine, where browsers allow passkeys without TLS: a local rehearsal
    # or scripts/screens.py. Anywhere else the passkey origin and the TAP authority need https.
    local = parsed.scheme == "http" and parsed.hostname in ("localhost", "127.0.0.1")
    if not (parsed.scheme == "https" or local) or not parsed.hostname:
        raise ValueError("COUNTERSIGN_PUBLIC_URL must be an https URL (or http://localhost)")
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


# One table of offline rates, 1 USD = this much, shared by the demo page and the house (Varunika's review, 11-05).
DEMO_RATES = {"EUR": "0.92", "GBP": "0.78", "AUD": "1.52", "SGD": "1.34", "USD": "1", "INR": "84.10"}


class TableFx:
    """Offline rate locks per shopper currency, for the house before sandbox keys exist."""

    RATES = {ccy: rate for ccy, rate in DEMO_RATES.items() if ccy != "USD"}

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
    oauth: OAuthServer | None = None  # C14, with a public URL to be its issuer
    keep_awake: bool = False  # ping our own public URL while running (COUNTERSIGN_KEEP_AWAKE)
    settler: Settler | None = None  # W3: the payments Airwallex's MCP makes, with /acp and Airwallex keys

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
    awake = environ.get("COUNTERSIGN_KEEP_AWAKE", "").strip().lower() in ("1", "true", "yes")
    if awake and not environ.get("COUNTERSIGN_PUBLIC_URL"):
        raise ValueError("COUNTERSIGN_KEEP_AWAKE needs COUNTERSIGN_PUBLIC_URL, the address it pings")
    client = airwallex_from_env(environ)
    settler = Settler(client, edge.state.acp.quote_for, signer, store) if client and acp else None
    return House(merchant, edge, signer, authority, acp is not None, store,
                 environ.get("AIRWALLEX_WEBHOOK_SECRET", "").encode(), mcp_token.encode(),
                 environ.get("COUNTERSIGN_PUBLIC_URL") or None, feed, lever_token.encode(),
                 client, sync, oauth_from_env(environ), awake, settler)


def _get(url: str) -> int:
    with urllib.request.urlopen(urllib.request.Request(url, headers={"User-Agent": "countersign-keep-awake"}),
                                timeout=60) as response:
        return response.status


def keep_awake(public_url: str, *, every: float = KEEP_AWAKE_EVERY, fetch: Callable[[str], int] = _get,
               stop: threading.Event | None = None) -> threading.Thread:
    """Ping our own /healthz through the public address until `stop` is set.

    The request leaves the process and comes back in through Render's proxy, so
    it counts as traffic and the free instance is never idle long enough to
    sleep. GitHub's scheduled keep-warm fired only every 3 to 10 hours
    (PROGRESS Open questions); this one runs as long as the service does. A
    failed ping is logged and the next one tried on time.
    """
    stop = stop or threading.Event()
    url = public_url.rstrip("/") + "/healthz"

    def run() -> None:
        while not stop.wait(every):
            try:
                fetch(url)
            except (OSError, urllib.error.URLError) as err:
                log.warning("keep-awake ping to %s failed: %s", url, err)

    thread = threading.Thread(target=run, name="keep-awake", daemon=True)
    thread.start()
    return thread


def webhook_app(house: House) -> FastAPI:
    """POST /webhooks/airwallex: settle a card payment from Airwallex's own record of it.

    The event is only a nudge. Its signature is checked (x-timestamp and
    x-signature, HMAC-SHA256 under the webhook secret), and then the merchant
    fetches the intent itself (AirwallexMerchant.confirm for the intents it
    started, Settler.settle for the ones Airwallex's MCP made) before anything
    is marked paid or refunded. A non-2xx answer makes Airwallex retry.
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
        if intent_id is None:
            return {"ignored": True}
        log.warning("webhook %s for %s", event.get("name"), intent_id)
        try:
            order = (await run_in_threadpool(house.merchant.confirm, intent_id)
                     if isinstance(house.merchant, AirwallexMerchant) else None)
            # Only the success event settles an MCP payment: the attempt events that follow name the same
            # intent and must not run the check again (Varunika's review, 11-05).
            settles = order is None and house.settler is not None and event.get("name") == "payment_intent.succeeded"
            settled = await run_in_threadpool(house.settler.settle, intent_id) if settles else None
        except (AirwallexError, *UNREACHABLE) as err:
            log.warning("webhook for %s not settled yet: %s", intent_id, err)
            return JSONResponse({"error": "Airwallex could not be reached; retry"}, status_code=503)
        if settled is not None and settled.status == "pending":
            # The success event arrived before Airwallex's own record says SUCCEEDED. Attempt events no
            # longer settle, so nothing else would retry it: a non-2xx makes Airwallex send it again.
            log.warning("%s: success event, but the intent is not SUCCEEDED yet; asking Airwallex to retry", intent_id)
            return JSONResponse({"error": "the intent is not SUCCEEDED yet; retry"}, status_code=503)
        if settled is not None:
            record_settlement(house, settled)
            return {"order_number": settled.checkout_id, "status": settled.status,
                    "receipt": settled.receipt is not None}
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


def record_settlement(house: House, settled: Settlement) -> None:
    """One console line when a payment Airwallex's MCP made is settled (W3)."""
    if settled.status == "paid":
        house.feed.add("payment", "go", f"Payment confirmed with Airwallex: {settled.checkout_id}",
                       f"{settled.amount} {settled.currency} · receipt signed · Airwallex's MCP",
                       order=settled.checkout_id)
    elif settled.status == "refunded":
        house.feed.add("payment", "no", f"Payment refunded: {settled.checkout_id}", "; ".join(settled.reasons),
                       order=settled.checkout_id)
    elif settled.status == "unverified":
        house.feed.add("payment", "ask", f"Payment not checked: {settled.checkout_id}", "; ".join(settled.reasons),
                       order=settled.checkout_id)


class Host:
    """ASGI: the house's paths to the house edge, the rest to the demo."""

    def __init__(self, demo: Any, house: House) -> None:
        self.demo = demo
        self.house = house
        self.hooks = webhook_app(house)
        self.mcp = mcp_app(house, house.mcp_token, house.public_url) if house.mcp_token else None
        self.oauth = oauth_app(house.oauth) if house.oauth is not None else None
        if house.keep_awake and house.public_url:
            keep_awake(house.public_url)

    async def __call__(self, scope, receive, send) -> None:
        if scope["type"] == "http" and scope["path"] == WEBHOOK_PATH:
            await self.hooks(scope, receive, send)
        elif scope["type"] == "http" and scope["path"] == MCP_PATH and self.mcp is not None:
            await self.mcp(scope, receive, send)
        elif scope["type"] == "http" and scope["path"] in OAUTH_PATHS and self.oauth is not None:
            await self.oauth(scope, receive, send)
        elif scope["type"] == "http" and self.house.owns(scope["method"], scope["path"]):
            await self.house.edge(scope, receive, send)
        else:
            await self.demo(scope, receive, send)
