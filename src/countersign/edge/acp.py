"""C10: the merchant's checkout API. Airwallex's merchant MCP calls our
`create_checkout`; our own MCP (web/merchant_mcp.py) calls the checkout sessions.

Shaped on the Agentic Commerce Protocol: create, update, read, complete and
cancel a checkout session. The API-Version header says 2026-04-17, but the
session body is still mostly the 2025-09-29 shape (`fulfillment_address`,
`fulfillment_option_id`, line items with `base_amount`, a top-level
`countersign` object, the warning codes over_ceiling, terms_changed and expired,
which the 2026-04-17 enums do not have). Moving the body to 2026-04-17 changes
C10, so it needs both builders. This is the only module that knows ACP JSON. It
turns sessions into the same Terms and Approval the direct TAP door uses, and
completing a session runs the same countersign(): the live offer is rebuilt
with a fresh rate lock and decide() charges, asks again or refuses. Nothing here
decides money.

The callers are platforms, not agents (Airwallex for create_checkout, our own
MCP for the sessions, signing in-process), so these routes sit outside the TAP
check and are authenticated by the shared HMAC secret instead (hmac_auth). The
body cap still applies.

Statuses on the wire are ACP's four only (not_ready_for_payment,
ready_for_payment, completed, canceled). The first reason was that Airwallex's
hosted MCP would sit in front of the session routes and might drop a status it
did not know; since 2026-11-03 it calls create_checkout instead, and the four
stay because they are all an ACP client can read. A session waiting for a
passkey signature over its terms is not_ready_for_payment, with an
approval_required message and the approval link in `links`.

What the shopper approved: until the shopper signs the terms themselves (U1),
the approval is the exact terms this API last returned, hashed when they were
returned. Completing means "these terms"; anything that has changed since is
asked again, never charged.

A currency we cannot price is an `invalid` message at `$.currency`. Airwallex
failing to lock a rate (anything but a 400 or 422 that names a currency, or no
usable answer at all) is a 503 `service_unavailable` / `rate_lock_unavailable`,
never reported as the shopper's currency. The same failure inside complete is
`payment_service_unavailable`, which never claims nothing was charged. A failed
create, update or complete leaves the session as it was.

With U1 on (COUNTERSIGN_REQUIRE_SHOPPER_SIGNATURE), the shopper signs those terms
with a passkey on the approval page, which calls two routes outside the HMAC
check, because the caller is the shopper's browser, not Airwallex:
GET /acp/approvals/{id} (what to sign, and the challenge) and POST
/acp/approvals/{id} (the assertion, checked by passkey.verify_assertion). The
link is the capability: a session id is 96 random bits. Every requote clears the
signature, so terms that changed are always signed again.

Airwallex's create_checkout (guide of 2026-11-03, airwallexdev.com/agentic-commerce)
is one signed POST that only prices: POST /acp/create_checkout takes the cart
and a currency and answers line items with what is available, one SHIPPING
method (standard and express), the totals in minor units without shipping, and
a permalink: the shopper's page for this order, /receipt/<checkout id>, where
the signed receipt appears once the payment settles (Airwallex hands it to the
agent as the completed session's order.permalink_url). Every call is a fresh quote under a new checkout
id, priced from the live catalogue with a fresh rate lock, and kept as a
QuoteRecord: Airwallex's MCP creates and charges the payment intent itself, so
the payment webhook (C9) finds the quote an intent belongs to with quote_for().
The checkout_sessions routes stay as they are for our own MCP. Every reading of
Airwallex's wire lives in this file (and the HMAC scheme in configuration), so
a correction never reaches the core.
"""

from __future__ import annotations

import http.client
import json
import logging
import secrets
import threading
from dataclasses import dataclass, field, fields, replace
from datetime import datetime, timedelta
from decimal import Decimal
from types import MappingProxyType
from typing import Any, Callable, Mapping
from urllib.parse import urlsplit

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse
from starlette.concurrency import run_in_threadpool

from .. import receipt as receipts
from ..airwallex import AirwallexError
from ..checkout import ReceiptStore, countersign, release
from ..decide import Decision, Outcome
from ..merchant import MerchantGateway
from ..terms import Approval, Terms, minor_units, money, utc
from . import hmac_auth, passkey
from .idempotency import IdempotencyStore, Stored, fingerprint

log = logging.getLogger("countersign.acp")

API_VERSION = "2026-04-17"
SUPPORTED_VERSIONS = (API_VERSION,)
APPROVAL_TTL = timedelta(minutes=15)
CURRENCY_REFUSED = (400, 422)  # Airwallex refusing the request; about the currency only if the error names one
SESSION_TTL = timedelta(hours=1)
PREFIX = "/acp/checkout_sessions"
CREATE_CHECKOUT = "/acp/create_checkout"  # the URL registered as apis.create_checkout at onboarding
QUOTE_TTL = timedelta(hours=1)  # a create_checkout quote holds as long as its rate lock, and at most this
# create_checkout's fulfillment: one SHIPPING method offering the catalogue's two delivery promises
# (merchant.py). Costs are in the merchant's currency and reach the checkout currency through the
# quote's own rate lock, rounded like any amount. Airwallex adds the chosen cost to our total.
SHIPPING_METHOD_ID = "shipping"
SHIPPING_DESTINATION_ID = "shipping_address"
SHIPPING_OPTIONS = (("standard", "Standard shipping (5-7 business days)", Decimal("0")),
                    ("express", "Express shipping (2 business days)", Decimal("9.99")))
APPROVALS = "/acp/approvals"  # the shopper's approval page (U1); no HMAC
ASSERTION_FIELDS = ("credential_id", "public_key_spki_der", "authenticator_data", "client_data_json", "signature")
# A16: Airwallex's payment attempt failure_code is one of authentication_failed, capture_failed,
# authorization_failed, provider_unavailable, system_unavailable, fraud_rejected
# (airwallex.com/docs/api/payments/payment_attempts). These mean the card was refused:
CARD_DECLINES = ("authorization_failed", "authentication_failed", "fraud_rejected")
# and these that Airwallex or the card provider could not process it. Anything else is reported as the code.
OUTAGES = ("provider_unavailable", "system_unavailable")


@dataclass
class AcpConfig:
    secret: bytes
    scheme: str = "airwallex"
    observe: bool = False  # log header names and body keys of refused calls, never values
    require_shopper_signature: bool = False  # U1: completion needs a passkey signature over the terms
    default_currency: str = "USD"
    public_url: str | None = None  # for the approval link and the receipt link

    @classmethod
    def from_env(cls, environ: Mapping[str, str]) -> "AcpConfig | None":
        """The config for a deployed edge, or None: without a secret the routes are not mounted at all.

        COUNTERSIGN_ACP_HMAC_SECRET   the secret given to Airwallex at onboarding (at least 32 bytes)
        COUNTERSIGN_ACP_HMAC_SCHEME   airwallex (default) | acp | guess
        COUNTERSIGN_ACP_OBSERVE       1 to log header names and body keys of refused calls
        COUNTERSIGN_REQUIRE_SHOPPER_SIGNATURE  1 once U1 is live
        COUNTERSIGN_ACP_CURRENCY      shopper currency when a session names none (default USD)
        COUNTERSIGN_PUBLIC_URL        the edge's public https URL
        """
        secret = environ.get("COUNTERSIGN_ACP_HMAC_SECRET", "")
        if not secret:
            return None
        if len(secret.encode()) < 32:
            raise ValueError("COUNTERSIGN_ACP_HMAC_SECRET must be at least 32 bytes")
        scheme = environ.get("COUNTERSIGN_ACP_HMAC_SCHEME", "airwallex")
        if scheme not in hmac_auth.SCHEMES:
            raise ValueError(f"COUNTERSIGN_ACP_HMAC_SCHEME must be one of {', '.join(hmac_auth.SCHEMES)}")
        flag = lambda name: environ.get(name, "").strip().lower() in ("1", "true", "yes")
        public_url = environ.get("COUNTERSIGN_PUBLIC_URL") or None
        if flag("COUNTERSIGN_REQUIRE_SHOPPER_SIGNATURE") and not public_url:
            raise ValueError("COUNTERSIGN_REQUIRE_SHOPPER_SIGNATURE needs COUNTERSIGN_PUBLIC_URL: "
                             "the shopper's passkey is checked against its origin")
        return cls(secret=secret.encode(), scheme=scheme, observe=flag("COUNTERSIGN_ACP_OBSERVE"),
                   require_shopper_signature=flag("COUNTERSIGN_REQUIRE_SHOPPER_SIGNATURE"),
                   default_currency=environ.get("COUNTERSIGN_ACP_CURRENCY", "USD").upper(),
                   public_url=public_url)

    @property
    def origin(self) -> str | None:
        """The approval page's origin, which the passkey assertion must name exactly."""
        if not self.public_url:
            return None
        parts = urlsplit(self.public_url)
        return f"{parts.scheme}://{parts.netloc}".lower()

    @property
    def rp_id(self) -> str | None:
        """The WebAuthn relying party id: the public host name."""
        return urlsplit(self.public_url).hostname if self.public_url else None


def to_minor(amount: Decimal, currency: str) -> int:
    """Integer minor units, or ValueError if the amount does not convert exactly."""
    if not Decimal(amount).is_finite():
        raise ValueError(f"{amount} {currency} is not a finite amount")
    scaled = Decimal(amount).scaleb(minor_units(currency))
    if scaled != scaled.to_integral_value():
        raise ValueError(f"{amount} {currency} is not a whole number of minor units")
    return int(scaled)


MAX_CEILING_MINOR = 10 ** 15  # the shopper's "up to": above any real purchase, below where Decimal quantizing fails


def from_minor(value: Any, currency: str) -> Decimal:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ValueError("amounts are non-negative integers in minor units")
    return Decimal(value).scaleb(-minor_units(currency))


class AcpError(Exception):
    type = "invalid_request"
    headers: Mapping[str, str] = MappingProxyType({})  # read only: shared by every instance

    def __init__(self, status: int, code: str, message: str, param: str | None = None, **extra: Any):
        super().__init__(message)
        self.status, self.code, self.message, self.param, self.extra = status, code, message, param, extra

    def body(self) -> dict[str, Any]:
        out = {"type": self.type, "code": self.code, "message": self.message}
        if self.param:
            out["param"] = self.param
        out.update(self.extra)
        return out


class RateLockUnavailable(AcpError):
    """Airwallex failed, not the request: a 503 to retry. A 5xx is never stored, so the retry runs again."""

    type = "service_unavailable"
    headers = MappingProxyType({"Retry-After": "5"})

    def __init__(self, detail: str):
        super().__init__(503, "rate_lock_unavailable",
                         f"No exchange rate could be locked right now ({detail}). Nothing was charged; "
                         "retry with the same Idempotency-Key.")


class PaymentServiceUnavailable(AcpError):
    """Airwallex failed while complete was rebuilding the offer or paying.

    Unlike a quote, we cannot know whether the payment started, so the message
    never says nothing was charged. What holds either way: the merchant key is
    acp:<session id>, so completing this session again never charges twice.
    """

    type = "service_unavailable"
    headers = MappingProxyType({"Retry-After": "5"})

    def __init__(self, detail: str):
        super().__init__(503, "payment_service_unavailable",
                         f"The payment service did not answer ({detail}). Completing this session again "
                         "never charges twice.")


class CancelUnavailable(AcpError):
    """Airwallex did not answer a cancel. The session here is unchanged; Airwallex may have heard it, so say only that."""

    type = "service_unavailable"
    headers = MappingProxyType({"Retry-After": "5"})

    def __init__(self) -> None:
        super().__init__(503, "payment_service_unavailable",
                         "Airwallex could not be reached to cancel the payment; nothing changed here. Try again.")


# What the Airwallex client lets out when there is no usable answer: no connection,
# a timeout, a cut-off body, a body that is not JSON. A ValueError in general is
# not here: that would hide our own bugs as an outage.
NO_ANSWER = (OSError, http.client.HTTPException, json.JSONDecodeError)


def _currency_named(body: Any) -> str | None:
    """The field or code in an Airwallex error body that names a currency, if any."""
    if not isinstance(body, dict):
        return None
    for key in ("source", "field", "param", "code"):
        value = body.get(key)
        if isinstance(value, str) and "currency" in value.lower():
            return value
    return None


@dataclass
class Session:
    id: str
    created: datetime
    currency: str  # shopper currency, upper case inside, lower case on the wire
    cart: dict[str, int] = field(default_factory=dict)
    ceiling_minor: int | None = None  # shopper's ceiling, if they set one above the quoted total
    ceiling_currency: str | None = None  # the currency they named it in; minor units mean nothing without it
    buyer: dict[str, Any] | None = None
    # Where the goods go, ACP's fields only. Stored and returned, never logged; not in the terms hash
    # (binding it is option (b) in PROGRESS Open questions, a C1 change for both builders).
    fulfillment_address: dict[str, str] | None = None
    payment_data: dict[str, Any] | None = None  # as sent on complete, card fields removed; never echoed or logged
    status: str = "not_ready_for_payment"  # ACP's enum only, plus "expired" internally
    terms: Terms | None = None
    approval: Approval | None = None
    shopper_signed: bytes | None = None  # the approval challenge the shopper's passkey signed (U1)
    shopper_assertion: dict[str, Any] | None = None  # that signature, as a receipt carries it (C13)
    messages: list[dict[str, Any]] = field(default_factory=list)
    order: Any = None
    receipt: dict[str, Any] | None = None
    lock: threading.Lock = field(default_factory=threading.Lock, repr=False)


def _snapshot(s: Session) -> dict[str, Any]:
    """Every field but the lock. Shallow is enough: requests replace values, never mutate them."""
    return {f.name: getattr(s, f.name) for f in fields(Session) if f.name != "lock"}


def _restore(s: Session, saved: dict[str, Any]) -> None:
    for name, value in saved.items():
        setattr(s, name, value)


@dataclass(frozen=True)
class QuoteRecord:
    """What one create_checkout call quoted, so the payment webhook can check an intent against it (C9, W3).

    total_minor is the items only, in minor units of `currency`. Fulfillment is
    never in it: Airwallex adds the selected option's cost (`options`) on top.
    terms, terms_hash and approval are None when nothing in the cart was
    available; the approval's ceiling is the items total.
    """

    checkout_id: str
    created: datetime
    expires_at: datetime  # the quote's rate lock expiry, or created + QUOTE_TTL if sooner or none
    currency: str
    line_items: tuple[dict[str, Any], ...]  # as answered: id, item_id, requested_quantity, available_quantity
    terms: Terms | None
    terms_hash: str | None
    approval: Approval | None
    total_minor: int
    options: dict[str, int]  # fulfillment option id -> cost in minor units of `currency`
    customer: dict[str, str] | None
    shipping_address: dict[str, str] | None


ADDRESS_FIELDS = ("name", "line_one", "line_two", "city", "state", "country", "postal_code")  # ACP's Address
MAX_ADDRESS_FIELD = 256
# Airwallex's create_checkout body (guide of 2026-11-03)
CUSTOMER_FIELDS = ("id", "email", "first_name", "last_name", "phone_number")
SHIPPING_ADDRESS_FIELDS = ("street", "city", "state", "postcode", "country_code")


def _read_text_fields(value: Any, name: str, allowed: tuple[str, ...]) -> dict[str, str] | None:
    """The allowed text fields of an optional object, as sent, or None. Errors name the field, never echo the value."""
    if value is None:
        return None
    if not isinstance(value, dict):
        raise AcpError(400, "invalid", f"{name} must be an object", f"$.{name}")
    out = {}
    for key in allowed:
        part = value.get(key)
        if part is None:
            continue
        if not isinstance(part, str) or len(part) > MAX_ADDRESS_FIELD:
            raise AcpError(400, "invalid", f"{name}.{key} must be text of at most "
                                           f"{MAX_ADDRESS_FIELD} characters", f"$.{name}.{key}")
        out[key] = part
    return out or None


def _read_address(value: Any) -> dict[str, str] | None:
    """ACP's address fields as sent, or None to clear it."""
    return _read_text_fields(value, "fulfillment_address", ADDRESS_FIELDS)


def _read_line_items(body: dict[str, Any]) -> list[dict[str, Any]]:
    """create_checkout's line_items as sent: Airwallex's line id, our variant id (the SKU), the quantity asked for."""
    items = body.get("line_items")
    if not isinstance(items, list) or not items:
        raise AcpError(400, "invalid", "line_items must be a non-empty list", "$.line_items")
    lines: list[dict[str, Any]] = []
    for i, item in enumerate(items):
        where = f"$.line_items[{i}]"
        if not isinstance(item, dict):
            raise AcpError(400, "invalid", "each line item must be an object", where)
        for name in ("id", "item_id"):
            value = item.get(name)
            if not isinstance(value, str) or not value or len(value) > MAX_ADDRESS_FIELD:
                raise AcpError(400, "invalid", f"{name} must be text of 1 to {MAX_ADDRESS_FIELD} characters",
                               f"{where}.{name}")
        if any(line["id"] == item["id"] for line in lines):
            raise AcpError(400, "invalid", "line item ids must be unique", f"{where}.id")
        qty = item.get("requested_quantity")
        if isinstance(qty, bool) or not isinstance(qty, int) or qty < 1:
            raise AcpError(400, "invalid", "requested_quantity must be a positive integer",
                           f"{where}.requested_quantity")
        lines.append({"id": item["id"], "item_id": item["item_id"], "requested_quantity": qty})
    return lines


def _read_items(body: dict[str, Any]) -> dict[str, int] | None:
    if "items" not in body:
        return None
    items = body["items"]
    if not isinstance(items, list):
        raise AcpError(400, "invalid", "items must be a list", "$.items")
    cart: dict[str, int] = {}
    for i, item in enumerate(items):
        if not isinstance(item, dict) or not isinstance(item.get("id"), str):
            raise AcpError(400, "invalid", "each item needs an id", f"$.items[{i}].id")
        qty = item.get("quantity")
        if isinstance(qty, bool) or not isinstance(qty, int) or qty < 1:
            raise AcpError(400, "invalid", "quantity must be a positive integer", f"$.items[{i}].quantity")
        cart[item["id"]] = cart.get(item["id"], 0) + qty
    return cart


def _read_currency(body: dict[str, Any]) -> str | None:
    cur = body.get("currency")
    if cur is None:
        return None
    if not isinstance(cur, str) or len(cur) != 3 or not (cur.isascii() and cur.isalpha()):
        raise AcpError(400, "invalid", "currency must be a three-letter code", "$.currency")
    return cur.upper()


# What managed_payment sends on complete is not published yet: a token or intent
# id for us to check, a charge already made, or nothing. We keep what arrives,
# minus anything that could be card data, so the first live call answers it.
_CARD_KEYS = {"number", "card_number", "pan", "cvc", "cvv", "cvc2", "cvv2", "security_code",
              "expiry", "expiry_month", "expiry_year", "exp_month", "exp_year"}


def _looks_like_card_number(value: str) -> bool:
    digits = value.replace(" ", "").replace("-", "")
    return digits.isdigit() and 12 <= len(digits) <= 19


def without_card_data(value: Any) -> Any:
    if isinstance(value, dict):
        return {k: without_card_data(v) for k, v in value.items() if str(k).lower() not in _CARD_KEYS}
    if isinstance(value, list):
        return [without_card_data(v) for v in value]
    if isinstance(value, str) and _looks_like_card_number(value):
        return "<removed>"
    return value


def _amount(value: Decimal, currency: str) -> int:
    return to_minor(money(value, currency), currency)


def _same_cart(approved: Any, live: Any) -> bool:
    """The same products in the same quantities: what moved, if anything, is a price."""
    return [(i.sku, i.quantity) for i in approved] == [(i.sku, i.quantity) for i in live]


class AcpApi:
    def __init__(self, merchant: MerchantGateway, config: AcpConfig, *, clock: Callable[[], datetime],
                 signer: receipts.MerchantSigner | None, store: ReceiptStore | None) -> None:
        self.merchant = merchant
        self.config = config
        self.clock = clock
        self.signer = signer
        self.store = store
        self.sessions: dict[str, Session] = {}
        self.idempotency = IdempotencyStore()
        self.passkeys: dict[str, bytes] = {}  # credential id -> public key; the first valid use binds it
        self.quotes: dict[str, list[QuoteRecord]] = {}  # create_checkout: checkout id -> its quotes, newest last

    # ---------------------------------------------------------------- the shape

    def view(self, s: Session) -> dict[str, Any]:
        cur = s.currency
        # "expired" is ours: on the wire a session past SESSION_TTL is canceled, with the reason.
        expired = s.status == "expired"
        failed, code = self.payment_failure(s)
        out: dict[str, Any] = {
            "id": s.id,
            # A refused card leaves the order open for another card: not completed until it is paid.
            "status": "canceled" if expired else "not_ready_for_payment" if failed else s.status,
            "currency": cur.lower(),
            "payment_provider": {"provider": "airwallex", "supported_payment_methods": ["card"]},
            "line_items": [],
            "totals": [],
            "fulfillment_options": [],
            "messages": list(s.messages) + ([{"type": "error", "code": "expired", "content_type": "plain",
                                               "content": "This checkout session expired. Nothing was charged; "
                                                          "start a new one."}] if expired else [])
                        + ([self._payment_failure_message(s, code)] if failed else []),
            "links": [],
        }
        if self.awaiting_shopper(s):
            out["links"].append({"type": "approval", "url": self.approval_link(s)})
        if s.buyer:
            out["buyer"] = s.buyer
        if s.fulfillment_address:
            out["fulfillment_address"] = dict(s.fulfillment_address)
        t = s.terms
        if t is not None:
            rate = t.fx.rate if t.fx else Decimal(1)
            for item in t.items:
                line = _amount(item.unit_price * item.quantity * rate, cur)
                out["line_items"].append({
                    "id": f"li_{item.sku}",
                    "item": {"id": item.sku, "quantity": item.quantity},
                    "base_amount": line, "discount": 0, "subtotal": line, "tax": 0, "total": line,
                })
            total = _amount(t.shopper_total, cur)
            out["totals"] = [
                {"type": "items_base_amount", "display_text": "Items", "amount": total},
                {"type": "total", "display_text": "Total", "amount": total},
            ]
            out["fulfillment_options"] = [{"type": "shipping", "id": "standard", "title": t.delivery,
                                           "subtotal": 0, "tax": 0, "total": 0}]
            out["fulfillment_option_id"] = "standard"
            out["countersign"] = {
                "terms_hash": s.approval.hash if s.approval else None,
                "ceiling": _amount(s.approval.ceiling, cur) if s.approval else None,
                "shopper_ceiling": s.ceiling_minor,  # what they said, in minor units; None if they named none
                "shopper_ceiling_currency": s.ceiling_currency.lower() if s.ceiling_currency else None,
                "merchant_currency": t.merchant_currency.lower(),
                "merchant_total": _amount(t.merchant_total, t.merchant_currency),
                "rate": format(t.fx.rate, "f") if t.fx else None,
                "rate_lock_id": t.fx.quote_id if t.fx else None,
                "approval_expires_at": utc(s.approval.expires_at).isoformat() if s.approval else None,
            }
        if s.order is not None:
            out["order"] = {
                "id": s.order.order_number,
                "checkout_session_id": s.id,
                # The shopper's page shows the order, its payment and the signed receipt.
                "permalink_url": f"{self.config.public_url.rstrip('/')}/pay/{s.id}"
                if self.config.public_url else None,
            }
            # A card order's receipt is signed later, when Airwallex confirms the payment (webhook or the
            # shopper page), into the store under this session's merchant key; the platform sees it here.
            late = self.store.get(f"acp:{s.id}") if s.receipt is None and self.store is not None else None
            out.setdefault("countersign", {})["receipt"] = s.receipt or late
            if failed:
                out["countersign"]["payment_failure"] = code  # Airwallex's code for the last attempt; None if none
        return out

    def open_payment(self, s: Session) -> Any:
        """The session's card order while its payment is still open (pending in Drop-in), else None."""
        if s.status != "completed" or s.order is None:
            return None
        order = self.merchant.find_order(f"acp:{s.id}")
        return order if order is not None and order.status == "pending" else None

    def payment_failure(self, s: Session) -> tuple[bool, str | None]:
        """(failed, code): did the last card attempt on the open payment fail (A16), and Airwallex's code.

        Only a merchant that takes cards in Drop-in keeps declines; the order stays
        pending on the same intent, so the shopper can pay it with another card.
        The code is None when Airwallex gave none.
        """
        key = f"acp:{s.id}"
        declines = getattr(self.merchant, "declines", {})
        if self.open_payment(s) is None or key not in declines:
            return False, None
        return True, declines[key]

    def _payment_failure_message(self, s: Session, code: str | None) -> dict[str, Any]:
        """Say what Airwallex reported and no more: a decline only on a card code, an outage only on an outage code."""
        where = (f" at {self.config.public_url.rstrip('/')}/pay/{s.id}" if self.config.public_url
                 else " on the payment page")
        if code in CARD_DECLINES:
            return {"type": "error", "code": "payment_declined", "content_type": "plain",
                    "content": f"The card was declined ({code}). Nothing was charged. The shopper can pay this "
                               f"order with another card{where}; nothing is retried automatically."}
        if code in OUTAGES:  # never tell the shopper their card was refused when Airwallex or a provider failed
            return {"type": "error", "code": "payment_unavailable", "content_type": "plain",
                    "content": f"The payment did not go through: Airwallex reported {code}, not a declined card. "
                               f"Nothing was charged. The shopper can try again{where}."}
        said = f"Airwallex reported {code}" if code else "Airwallex gave no reason"
        return {"type": "error", "code": "payment_failed", "content_type": "plain",
                "content": f"The card payment did not go through: {said}. Nothing was charged. "
                           f"The shopper can try again{where}; nothing is retried automatically."}

    # ---------------------------------------------------------------- the logic

    def _quote(self, s: Session, now: datetime) -> None:
        """Price the cart now and record it as what the shopper is being shown.

        Priced before anything is cleared, so an outage (RateLockUnavailable)
        leaves the session as it was and a retried complete still finds its terms.
        """
        terms, problem = self._price(s.cart, s.currency, s.id, now) if s.cart else (None, None)
        s.messages = []
        s.terms = s.approval = None
        s.shopper_signed = s.shopper_assertion = None  # a signature never carries over to new terms
        if terms is None:
            s.status, s.messages = "not_ready_for_payment", [problem] if problem else []
            return
        ceiling = terms.shopper_total
        named = self._ceiling(s)
        if named is not None and named[1] == s.currency:
            ceiling = max(ceiling, named[0])
        s.terms = terms
        s.approval = Approval.grant(terms, ceiling, self._approver(s), now, now + APPROVAL_TTL)
        if self.awaiting_shopper(s):
            s.status = "not_ready_for_payment"
            s.messages = [{"type": "info", "code": "approval_required", "content_type": "plain",
                           "content": f"The shopper approves these exact terms here: {self.approval_link(s)}"}]
        elif self.over_ceiling(s):
            s.status = "not_ready_for_payment"
            s.messages = [self._over_ceiling_message(s)]
        else:
            s.status = "ready_for_payment"

    def _price(self, cart: dict[str, int], currency: str, ref: str,
               now: datetime) -> tuple[Terms | None, dict[str, Any] | None]:
        """Live terms for the cart, or the message that says why there are none. `ref` names the call in logs.

        Anything that is Airwallex not answering usably raises RateLockUnavailable,
        so an outage is never reported as the shopper's cart or currency.
        """
        try:
            return self.merchant.offer(cart, currency, now), None
        except KeyError as err:
            missing = err.args[0] if err.args else None
            if missing not in cart:  # a field absent from Airwallex's answer, not a product
                raise self._no_rate_lock(ref, "Airwallex's answer was missing a field", f"KeyError {missing!r}") from None
            return None, {"type": "error", "code": "invalid", "param": "$.items",
                          "content_type": "plain", "content": f"no product with id {missing}"}
        except LookupError as err:
            # Out of stock and "no rate for this currency" are both LookupErrors.
            # In the merchant's own currency no rate lock is needed, so if the cart
            # prices there, the stock is fine and the currency is what failed.
            if currency != self.merchant.currency and self._prices_without_fx(cart, now):
                return None, self._unpriceable(currency, "this merchant has no rate for it")
            return None, {"type": "error", "code": "out_of_stock", "param": "$.items",
                          "content_type": "plain", "content": str(err)}
        except AirwallexError as err:
            # A 400 or 422 is Airwallex saying no to the request, but this path also
            # carries the login and our own fields (validity_period, request_id).
            # Only an error that names a currency is about the shopper's currency;
            # our key, our fields, their limits or their servers never are.
            named = _currency_named(err.body)
            code = err.body.get("code") if isinstance(err.body, dict) else None
            if err.status in CURRENCY_REFUSED and named is not None:
                why = f"{code} at {named}" if code and code != named else named
                return None, self._unpriceable(currency, f"Airwallex would not lock a rate ({why})")
            raise self._no_rate_lock(ref, f"Airwallex answered HTTP {err.status}",
                                     f"Airwallex HTTP {err.status} {code or ''}") from None
        except NO_ANSWER as err:
            raise self._no_rate_lock(ref, f"Airwallex could not be reached: {type(err).__name__}",
                                     type(err).__name__) from None

    def _no_rate_lock(self, ref: str, detail: str, logged: str) -> RateLockUnavailable:
        log.warning("acp quote %s: no rate lock, %s", ref, logged)
        return RateLockUnavailable(detail)

    def _prices_without_fx(self, cart: dict[str, int], now: datetime) -> bool:
        try:
            self.merchant.offer(cart, self.merchant.currency, now)
        except LookupError:
            return False
        return True

    def _unpriceable(self, currency: str, why: str) -> dict[str, Any]:
        return {"type": "error", "code": "invalid", "param": "$.currency", "content_type": "plain",
                "content": f"This merchant cannot price {currency} from {self.merchant.currency}: {why}. "
                           "Choose another currency. Nothing was charged."}

    def _ceiling(self, s: Session) -> tuple[Decimal, str] | None:
        """The shopper's ceiling and the currency they named it in, or None if they named none."""
        if s.ceiling_minor is None:
            return None
        cur = s.ceiling_currency or s.currency
        return from_minor(s.ceiling_minor, cur), cur

    def over_ceiling(self, s: Session) -> bool:
        """The shopper named the most they will pay, and these terms cost more.

        The approval above still carries a ceiling of at least the total (an
        approval below its own price cannot exist), so only this check keeps
        the shopper's number binding. With U1 on, the shopper's passkey on the
        new exact terms is their new yes; without it, only a new ceiling sent
        as an update is.

        A ceiling named in another currency holds too. The session moved
        currency after the shopper named it, and their number says nothing
        about a price in the new one: paise read as cents allowed about 84
        times what the shopper meant.
        """
        named = self._ceiling(s)
        return (named is not None and s.terms is not None
                and (named[1] != s.currency or s.terms.shopper_total > named[0]))

    def _over_ceiling_message(self, s: Session) -> dict[str, Any]:
        named = self._ceiling(s)
        assert s.terms is not None and named is not None
        cur, total = s.currency, s.terms.shopper_total
        ceiling, named_in = named
        if named_in == cur:
            content = (f"The total is {total} {cur}, over the shopper's ceiling of {ceiling} {cur}. "
                       "Nothing was charged. It goes ahead only with a new ceiling from the shopper, "
                       "sent as an update.")
        else:
            content = (f"The total is {total} {cur}, and the shopper's ceiling of {ceiling} {named_in} "
                       f"was named before the checkout moved to {cur}. Nothing was charged. It goes ahead "
                       f"only with a new ceiling in {cur} from the shopper, sent as an update.")
        return {"type": "warning", "code": "over_ceiling", "resolution": "requires_buyer_review",
                "param": "$.ceiling", "content_type": "plain", "content": content,
                "ceiling": s.ceiling_minor, "ceiling_currency": named_in.lower(),
                "current_amount": _amount(total, cur)}

    def awaiting_shopper(self, s: Session) -> bool:
        """U1 is on and the shopper has not signed the terms now on the session."""
        return (self.config.require_shopper_signature and s.approval is not None
                and s.status not in ("completed", "canceled", "expired")
                and s.shopper_signed != self.challenge(s))

    def challenge(self, s: Session) -> bytes:
        """What the shopper's passkey signs: these terms, in this session, until this expiry."""
        assert s.approval is not None
        return passkey.approval_challenge(s.id, s.approval.hash, s.approval.expires_at)

    def approval_link(self, s: Session) -> str:
        base = self.config.public_url.rstrip("/") if self.config.public_url else ""
        return f"{base}/approve/{s.id}"

    def _approver(self, s: Session) -> str:
        email = (s.buyer or {}).get("email")
        return f"acp:{email}" if isinstance(email, str) and email else "acp:platform"

    def _reask_messages(self, s: Session, decision: Decision, before: Terms) -> list[dict[str, Any]]:
        cur = s.currency
        msgs: list[dict[str, Any]] = []
        for change in decision.changes:
            if change.field == "stock":
                msgs.append({"type": "error", "code": "out_of_stock", "param": "$.line_items",
                             "content_type": "plain", "content": str(change.live)})
            elif change.field in ("shopper_total", "merchant_total"):
                pass  # reported once below, in the shopper's currency
            elif change.field == "items" and _same_cart(change.approved, change.live):
                pass  # only unit prices moved: that is the total, reported once below
            else:
                msgs.append({"type": "warning", "code": "terms_changed", "resolution": "requires_buyer_review",
                             "param": f"$.{change.field}", "content_type": "plain",
                             "content": f"{change.field} changed since the terms were shown"})
        after = s.terms
        if after is not None and after.shopper_total != before.shopper_total:
            msgs.append({"type": "warning", "code": "price_change", "resolution": "requires_buyer_review",
                         "param": "$.totals", "content_type": "plain",
                         "content": f"The total moved from {before.shopper_total} to {after.shopper_total} {cur}. "
                                    "Nothing was charged.",
                         "previous_amount": _amount(before.shopper_total, cur),
                         "current_amount": _amount(after.shopper_total, cur)})
        elif decision.reason == "the approval has expired":
            msgs.append({"type": "warning", "code": "expired", "resolution": "requires_buyer_review",
                         "content_type": "plain", "content": "The approval expired; the terms were quoted again."})
        if not msgs:
            msgs.append({"type": "warning", "code": "terms_changed", "resolution": "requires_buyer_review",
                         "content_type": "plain", "content": decision.reason})
        return msgs

    def create(self, body: dict[str, Any]) -> tuple[int, dict[str, Any]]:
        now = self.clock()
        cart = _read_items(body) or {}
        s = Session(id=f"cs_{secrets.token_hex(12)}", created=now,
                    currency=_read_currency(body) or self.config.default_currency.upper(), cart=cart)
        self._apply_extras(s, body)
        self._quote(s, now)
        self.sessions[s.id] = s
        return 201, self.view(s)

    def _apply_extras(self, s: Session, body: dict[str, Any]) -> None:
        if isinstance(body.get("buyer"), dict):
            s.buyer = body["buyer"]
        if "fulfillment_address" in body:
            s.fulfillment_address = _read_address(body["fulfillment_address"])
        ceiling = body.get("ceiling")  # ours, not ACP: the shopper's "up to" in minor units
        if ceiling is not None:
            try:
                from_minor(ceiling, s.currency)
            except ValueError as err:
                raise AcpError(400, "invalid", str(err), "$.ceiling") from None
            if ceiling > MAX_CEILING_MINOR:
                raise AcpError(400, "invalid", "the ceiling is out of range", "$.ceiling")
            s.ceiling_minor, s.ceiling_currency = ceiling, s.currency

    def _session(self, session_id: str) -> Session:
        s = self.sessions.get(session_id)
        if s is None:
            raise AcpError(404, "not_found", "no checkout session with this id")
        if s.status not in ("completed", "canceled") and self.clock() - s.created > SESSION_TTL:
            s.status = "expired"
        return s

    def get(self, session_id: str) -> tuple[int, dict[str, Any]]:
        return 200, self.view(self._session(session_id))

    def update(self, session_id: str, body: dict[str, Any]) -> tuple[int, dict[str, Any]]:
        s = self._session(session_id)
        with s.lock:
            self._open(s)
            # A refused or failed update changes nothing: otherwise the cart from a
            # 400 is what the next quote prices, and the shopper never asked for it.
            saved = _snapshot(s)
            try:
                cart = _read_items(body)
                if cart is not None:
                    s.cart = cart
                currency = _read_currency(body)
                if currency is not None:
                    s.currency = currency
                self._apply_extras(s, body)
                self._quote(s, self.clock())
            except Exception:
                _restore(s, saved)
                raise
            return 200, self.view(s)

    def _open(self, s: Session) -> None:
        failed, code = self.payment_failure(s)
        if failed:  # the wire says not_ready_for_payment, so say why it still cannot change (A16)
            said = self._payment_failure_message(s, code)
            raise AcpError(405, said["code"], said["content"] + " These terms cannot change while their payment "
                           "is open: the shopper pays there, or cancel this checkout and start a new one.")
        if s.status in ("completed", "canceled", "expired"):
            raise AcpError(405, "invalid_state", f"the session is {s.status}")

    def complete(self, session_id: str, body: dict[str, Any]) -> tuple[int, dict[str, Any]]:
        s = self._session(session_id)
        with s.lock:
            if s.status == "completed":
                return 200, self.view(s)  # completing twice never charges twice
            self._open(s)
            saved = _snapshot(s)
            try:
                return self._complete(s, body)
            except Exception:
                _restore(s, saved)  # a failed complete leaves the session as it was
                raise

    def _complete(self, s: Session, body: dict[str, Any]) -> tuple[int, dict[str, Any]]:
        """complete() under the session lock; anything it raises is undone by the caller."""
        if isinstance(body.get("buyer"), dict):
            s.buyer = body["buyer"]
        payment = body.get("payment_data")
        if isinstance(payment, dict):
            s.payment_data = without_card_data(payment)
        # WARNING so it shows under the host's default log level; presence and key names only.
        log.warning("acp complete %s: payment_data %s", s.id,
                    f"present, keys {sorted(map(str, payment))}" if isinstance(payment, dict)
                    else "absent" if payment is None else f"present but a {type(payment).__name__}")
        if s.approval is None or s.terms is None:
            raise AcpError(400, "invalid_state", "the session has nothing ready to pay for")
        if self.awaiting_shopper(s):
            s.status = "not_ready_for_payment"
            s.messages = [{"type": "error", "code": "approval_required", "content_type": "plain",
                           "content": "The shopper has not approved these exact terms. Nothing was charged."}]
            return 200, self.view(s)
        if self.over_ceiling(s) and not self.config.require_shopper_signature:
            s.status, s.messages = "not_ready_for_payment", [self._over_ceiling_message(s)]
            return 200, self.view(s)

        now = self.clock()
        before = s.terms
        approval = s.approval
        if s.shopper_assertion is not None and s.shopper_signed == self.challenge(s):
            approval = replace(approval, shopper_assertion=s.shopper_assertion)  # C13: into the receipt
        try:
            result = countersign(self.merchant, approval, f"acp:{s.id}", now,
                                 agent=None, signer=self.signer, store=self.store)
        except (AirwallexError, *NO_ANSWER) as err:
            # countersign() rebuilt the offer and may have started the payment: we
            # cannot say nothing was charged, only that a retry never charges twice.
            status = f"HTTP {err.status}" if isinstance(err, AirwallexError) else type(err).__name__
            log.warning("acp complete %s: no answer from the payment service, %s", s.id, status)
            detail = (f"Airwallex answered {status}" if isinstance(err, AirwallexError)
                      else f"Airwallex could not be reached: {status}")
            raise PaymentServiceUnavailable(detail) from None
        d = result.decision
        if d.outcome is Outcome.PROCEED and result.order is not None:
            s.status, s.order, s.receipt, s.messages = "completed", result.order, result.receipt, []
        elif d.outcome is Outcome.REASK:
            self._quote(s, now)  # the new terms the shopper must look at again
            if s.terms is not None:  # requoted; not a cart that can no longer be priced
                s.messages = self._reask_messages(s, d, before) + s.messages
        else:
            s.status = "not_ready_for_payment"
            s.messages = [{"type": "error", "code": "invalid", "content_type": "plain",
                           "content": f"Refused: {d.reason}. Nothing was charged."}]
        return 200, self.view(s)

    # ---------------------------------------------------------------- create_checkout (Airwallex's MCP)

    def create_checkout(self, body: dict[str, Any]) -> tuple[int, dict[str, Any]]:
        """Price the cart now, keep the quote, and answer Airwallex's create_checkout.

        Called when the shopper starts or updates checkout. Nothing here charges:
        Airwallex's MCP creates the payment intent from this answer. A line we
        cannot fill is answered with what is available (0 for an unknown or sold
        out item), never an error, and the rest is priced with a fresh rate lock.
        """
        now = self.clock()
        lines = _read_line_items(body)
        currency = _read_currency(body)
        if currency is None:
            raise AcpError(400, "invalid", "currency is required", "$.currency")
        customer = _read_text_fields(body.get("customer"), "customer", CUSTOMER_FIELDS)
        address = _read_text_fields(body.get("shipping_address"), "shipping_address", SHIPPING_ADDRESS_FIELDS)
        checkout_id = f"chk_{secrets.token_hex(12)}"
        # Key names only: whether Airwallex sends more than its guide shows (an id on an update?) is
        # what the first live calls tell us. Values can hold the shopper's details, so never those.
        log.warning("acp create_checkout %s: %d lines in %s, body keys %s", checkout_id, len(lines), currency,
                    sorted(map(str, body)))
        lines, cart = self._available(lines)
        terms, problem = self._price(cart, currency, checkout_id, now) if cart else (None, None)
        if problem is not None:  # a currency we cannot price, or stock that moved while pricing (retry)
            raise AcpError(409 if problem["code"] == "out_of_stock" else 400, problem["code"], problem["content"],
                           problem.get("param"))
        expires, approval = now + QUOTE_TTL, None
        if terms is not None:
            if terms.fx is not None:
                expires = min(expires, utc(terms.fx.expires_at))
            email = (customer or {}).get("email")
            approval = Approval.grant(terms, terms.shopper_total, f"acp:{email}" if email else "acp:platform",
                                      now, expires)
        total = _amount(terms.shopper_total, currency) if terms is not None else 0
        out: dict[str, Any] = {"id": checkout_id, "line_items": lines}
        options: dict[str, int] = {}
        if terms is not None:  # nothing to ship, and no rate to price shipping with, when nothing is available
            rate = terms.fx.rate if terms.fx else Decimal(1)
            options = {oid: _amount(cost * rate, currency) for oid, _, cost in SHIPPING_OPTIONS}
            out["fulfillment"] = {"methods": [{
                "id": SHIPPING_METHOD_ID,
                "type": "SHIPPING",
                "line_item_ids": [line["id"] for line in lines],  # every line of the cart, as the rules ask
                "destinations": [{"id": SHIPPING_DESTINATION_ID, "address": dict(address)}] if address else [],
                "options": [{"id": oid, "title": title, "cost": options[oid]} for oid, title, _ in SHIPPING_OPTIONS],
            }]}
        out["totals"] = [{"type": "subtotal", "amount": total, "display_text": "Subtotal"},
                         {"type": "total", "amount": total, "display_text": "Total"}]
        link = self._permalink(checkout_id, lines)
        if link is not None:
            out["permalink_url"] = link
        self.quotes.setdefault(checkout_id, []).append(QuoteRecord(
            checkout_id=checkout_id, created=now, expires_at=expires, currency=currency,
            line_items=tuple(dict(line) for line in lines), terms=terms,
            terms_hash=approval.hash if approval else None, approval=approval, total_minor=total,
            options=options, customer=customer, shipping_address=address))
        return 200, out

    def quote_for(self, checkout_id: str) -> QuoteRecord | None:
        """The newest quote kept under this checkout id, or None."""
        found = self.quotes.get(checkout_id)
        return found[-1] if found else None

    def _available(self, lines: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], dict[str, int]]:
        """Each line with its available_quantity, and the cart of what is available.

        Two lines for one SKU share its stock, in the order they were sent.
        """
        left: dict[str, int] = {}
        cart: dict[str, int] = {}
        out = []
        for line in lines:
            sku = line["item_id"]
            if sku not in left:
                product = self.merchant.catalog.get(sku)  # both merchants keep the live catalogue here
                left[sku] = max(product.stock, 0) if product is not None else 0
            n = min(line["requested_quantity"], left[sku])
            left[sku] -= n
            if n:
                cart[sku] = cart.get(sku, 0) + n
            out.append({**line, "available_quantity": n})
        return out, cart

    def _permalink(self, checkout_id: str, lines: list[dict[str, Any]]) -> str | None:
        """The shopper's page for this order, where its signed receipt appears once paid. None if nothing
        in the cart is ours to sell."""
        if not self.config.public_url or not any(line["item_id"] in self.merchant.catalog for line in lines):
            return None
        return f"{self.config.public_url.rstrip('/')}/receipt/{checkout_id}"

    def cancel(self, session_id: str) -> tuple[int, dict[str, Any]]:
        s = self._session(session_id)
        with s.lock:
            if self.open_payment(s) is not None and hasattr(self.merchant, "cancel_payment"):
                return self._cancel_open_payment(s)
            self._open(s)
            s.status, s.messages = "canceled", []
            return 200, self.view(s)

    def _cancel_open_payment(self, s: Session) -> tuple[int, dict[str, Any]]:
        """Cancel a card order still open in Drop-in, declined or not (A16). Airwallex's record decides the answer."""
        order = self.merchant.cancel_payment(f"acp:{s.id}")
        if order is None or order.status == "pending":
            raise CancelUnavailable()
        if order.status == "paid":
            # A card paid before the cancel landed, so the order stands. Sign its held receipt the way the
            # webhook and the shopper page do (checkout.release; safe to repeat), so this answer carries it.
            s.order = order
            if self.signer is not None and self.store is not None:
                s.receipt = release(order, self.signer, self.store)
            return 200, self.view(s)
        s.status, s.order, s.receipt, s.messages = "canceled", None, None, []
        if order.status == "refunded":  # a card paid an amount the countersign did not allow; it went back
            s.messages = [{"type": "warning", "code": "payment_refunded", "content_type": "plain",
                           "content": "A card payment for this checkout did not match the approved terms, so it "
                                      "was refunded in full. The checkout is canceled."}]
        return 200, self.view(s)

    # ---------------------------------------------------------------- U1, the shopper's passkey

    def _approvable(self, s: Session) -> tuple[str, str]:
        self._open(s)
        if s.approval is None or s.terms is None:
            raise AcpError(409, "invalid_state", "the session has no terms to approve yet")
        if not self.config.origin or not self.config.rp_id:
            raise AcpError(409, "invalid_state", "passkey approval needs COUNTERSIGN_PUBLIC_URL")
        return self.config.origin, self.config.rp_id

    def approval_request(self, session_id: str) -> tuple[int, dict[str, Any]]:
        """What the approval page shows, and the challenge its passkey call signs. Never the buyer's details."""
        s = self._session(session_id)
        with s.lock:
            origin, rp_id = self._approvable(s)
            t, a, cur = s.terms, s.approval, s.currency
            challenge = self.challenge(s)
            return 200, {
                "session_id": s.id,
                "status": s.status,
                "signed": s.shopper_signed == challenge,
                "terms": {
                    "merchant_id": t.merchant_id,
                    "items": [{"id": i.sku, "title": i.title, "quantity": i.quantity} for i in t.items],
                    "delivery": t.delivery,
                    "currency": cur.lower(),
                    "total": _amount(t.shopper_total, cur),
                    "ceiling": _amount(a.ceiling, cur),
                    "merchant_currency": t.merchant_currency.lower(),
                    "merchant_total": _amount(t.merchant_total, t.merchant_currency),
                    "rate": format(t.fx.rate, "f") if t.fx else None,
                    "terms_hash": a.hash,
                    "expires_at": utc(a.expires_at).isoformat(),
                },
                "webauthn": {"challenge": passkey.b64url(challenge), "rp_id": rp_id, "origin": origin,
                             "user_verification": "preferred"},
            }

    def approve(self, session_id: str, body: dict[str, Any]) -> tuple[int, dict[str, Any]]:
        """U1: the shopper's passkey has signed this session's current challenge. Nothing changes unless it verifies."""
        s = self._session(session_id)
        with s.lock:
            origin, rp_id = self._approvable(s)
            raw: dict[str, bytes] = {}
            for name in ASSERTION_FIELDS:
                try:
                    raw[name] = passkey.b64url_decode(body.get(name))
                except ValueError:
                    raise AcpError(400, "invalid", f"{name} must be base64url", f"$.{name}") from None
            credential = passkey.b64url(raw["credential_id"])
            known = self.passkeys.get(credential)
            if known is not None and known != raw["public_key_spki_der"]:
                raise AcpError(400, "assertion_invalid", "this credential is registered with a different key")
            challenge = self.challenge(s)
            result = passkey.verify_assertion(
                public_key_spki_der=raw["public_key_spki_der"], authenticator_data=raw["authenticator_data"],
                client_data_json=raw["client_data_json"], signature=raw["signature"],
                expected_challenge=challenge, origin=origin, rp_id=rp_id)
            if isinstance(result, str):
                raise AcpError(400, "assertion_invalid", result)
            self.passkeys.setdefault(credential, raw["public_key_spki_der"])
            s.shopper_signed = challenge
            # Everything an offline check needs to recompute the challenge and verify the signature.
            s.shopper_assertion = {**{name: passkey.b64url(value) for name, value in raw.items()},
                                   "session_id": s.id, "rp_id": rp_id, "origin": origin}
            s.status, s.messages = "ready_for_payment", []
            return 200, {"session_id": s.id, "status": s.status, "signed": True, "terms_hash": s.approval.hash,
                         "user_verified": result.user_verified}


def router(api: AcpApi) -> APIRouter:
    r = APIRouter()
    cfg = api.config

    async def handle(request: Request, action: Callable[[Any], tuple[int, dict[str, Any]]], *, post: bool,
                     versioned: bool = True):
        raw = await request.body()
        reason = hmac_auth.verify(dict(request.headers), raw, cfg.secret, scheme=cfg.scheme)
        if reason is not None:
            if cfg.observe:
                observe(request, raw)
            return JSONResponse({"type": "invalid_request", "code": "unauthorized", "message": reason}, status_code=401)

        echo = {"Request-Id": request.headers["request-id"]} if "request-id" in request.headers else {}
        version = request.headers.get("api-version") if versioned else None  # ACP's header, not Airwallex's
        if version is not None and version not in SUPPORTED_VERSIONS:
            return JSONResponse({"type": "invalid_request", "code": "unsupported_api_version",
                                 "message": f"API-Version {version} is not supported",
                                 "supported_versions": list(SUPPORTED_VERSIONS)}, status_code=400, headers=echo)
        try:
            body = json.loads(raw) if raw else {}
        except (ValueError, RecursionError):  # RecursionError: nested past json's limit, under the body cap
            return JSONResponse(AcpError(400, "invalid", "the body is not JSON").body(), status_code=400, headers=echo)
        if not isinstance(body, dict):
            return JSONResponse(AcpError(400, "invalid", "the body must be a JSON object").body(),
                                status_code=400, headers=echo)

        if not post:
            return await _run(action, body, echo)

        key = request.headers.get("idempotency-key")
        if not key:
            return JSONResponse(AcpError(400, "idempotency_key_required", "every POST needs an Idempotency-Key").body(),
                                status_code=400, headers=echo)
        fp = fingerprint(request.method, request.url.path, body)
        state, stored = api.idempotency.begin(key, fp)
        if state == "replay":
            return JSONResponse(stored.body, status_code=stored.status,
                                headers={**stored.headers, **echo, "Idempotent-Replayed": "true"})
        if state == "conflict":
            return JSONResponse(AcpError(422, "idempotency_conflict",
                                         "this Idempotency-Key was used for a different request").body(),
                                status_code=422, headers=echo)
        if state == "in_flight":
            return JSONResponse(AcpError(409, "idempotency_in_flight",
                                         "a request with this Idempotency-Key is still running").body(),
                                status_code=409, headers={**echo, "Retry-After": "1"})
        try:
            response = await _run(action, body, echo)
        except BaseException:
            api.idempotency.abandon(key)
            raise
        api.idempotency.finish(key, Stored(response.status_code, json.loads(response.body), {}))
        return response

    async def _run(action, body, echo) -> JSONResponse:
        try:
            status, payload = await run_in_threadpool(action, body)
        except AcpError as err:
            return JSONResponse(err.body(), status_code=err.status, headers={**echo, **err.headers})
        return JSONResponse(payload, status_code=status, headers=echo)

    @r.post(CREATE_CHECKOUT)
    async def create_checkout(request: Request):
        # Airwallex's own contract, not ACP's: no Idempotency-Key (a repeat is a fresh quote under a
        # new id) and no API-Version to check.
        return await handle(request, api.create_checkout, post=False, versioned=False)

    @r.post(PREFIX)
    async def create_session(request: Request):
        return await handle(request, api.create, post=True)

    @r.get(PREFIX + "/{session_id}")
    async def get_session(session_id: str, request: Request):
        return await handle(request, lambda body: api.get(session_id), post=False)

    @r.post(PREFIX + "/{session_id}")
    async def update_session(session_id: str, request: Request):
        return await handle(request, lambda body: api.update(session_id, body), post=True)

    @r.post(PREFIX + "/{session_id}/complete")
    async def complete_session(session_id: str, request: Request):
        return await handle(request, lambda body: api.complete(session_id, body), post=True)

    @r.post(PREFIX + "/{session_id}/cancel")
    async def cancel_session(session_id: str, request: Request):
        return await handle(request, lambda body: api.cancel(session_id), post=True)

    async def shopper(request: Request, action: Callable[[Any], tuple[int, dict[str, Any]]]) -> JSONResponse:
        # The approval page's calls come from the shopper's browser, so there is no
        # HMAC here: the session id in the link is the capability, and a passkey
        # signature is the only thing that changes a session.
        raw = await request.body()
        try:
            body = json.loads(raw) if raw else {}
        except (ValueError, RecursionError):
            return JSONResponse(AcpError(400, "invalid", "the body is not JSON").body(), status_code=400)
        if not isinstance(body, dict):
            return JSONResponse(AcpError(400, "invalid", "the body must be a JSON object").body(), status_code=400)
        return await _run(action, body, {})

    @r.get(APPROVALS + "/{session_id}")
    async def approval_request(session_id: str, request: Request):
        return await shopper(request, lambda body: api.approval_request(session_id))

    @r.post(APPROVALS + "/{session_id}")
    async def approve(session_id: str, request: Request):
        return await shopper(request, lambda body: api.approve(session_id, body))

    return r


def observe(request: Request, raw: bytes) -> None:
    """U4: learn the caller's contract from a refused call without keeping anything sensitive.

    Logs header NAMES and top-level body KEYS only. Values can hold the secret,
    a card number or an address, so they are never logged.
    """
    try:
        parsed = json.loads(raw) if raw else {}
        keys = sorted(parsed) if isinstance(parsed, dict) else [type(parsed).__name__]
    except (ValueError, RecursionError):
        keys = ["<not json>"]
    log.warning("acp observe: %s %s headers=%s body_keys=%s", request.method, request.url.path,
                sorted(k.lower() for k in request.headers.keys()), keys)


def is_acp(path: str) -> bool:
    """The checkout API (HMAC in its routes) and the approval page's calls (passkey): outside TAP."""
    return path == CREATE_CHECKOUT or any(path == p or path.startswith(p + "/") for p in (PREFIX, APPROVALS))
