"""C10: the checkout API Airwallex's merchant MCP calls (our `create_checkout`).

Shaped on the Agentic Commerce Protocol, version 2026-04-17: create, update,
read, complete and cancel a checkout session. This is the only module that
knows ACP JSON. It turns sessions into the same Terms and Approval the direct
TAP door uses, and completing a session runs the same countersign(): the live
offer is rebuilt with a fresh rate lock and decide() charges, asks again or
refuses. Nothing here decides money.

The caller is a platform (Airwallex), not an agent, so these routes sit outside
the TAP check and are authenticated by the shared HMAC secret instead
(hmac_auth). The body cap still applies.

Statuses on the wire are ACP's four only (not_ready_for_payment,
ready_for_payment, completed, canceled): Airwallex's hosted MCP sits between us
and the agent and may drop a session whose status it does not know. A session
waiting for the shopper's own signature is not_ready_for_payment, with an
approval_required message and the approval link in `links`.

What the shopper approved: until the shopper signs the terms themselves (U1),
the approval is the exact terms this API last returned, hashed when they were
returned. Completing means "these terms"; anything that has changed since is
asked again, never charged.

Airwallex has not published its exact create_checkout contract. Every guess
about it lives in this file (and the HMAC scheme in configuration), so a
correction never reaches the core.
"""

from __future__ import annotations

import json
import logging
import secrets
import threading
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from decimal import Decimal
from typing import Any, Callable, Mapping

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse
from starlette.concurrency import run_in_threadpool

from .. import receipt as receipts
from ..checkout import ReceiptStore, countersign
from ..decide import Decision, Outcome
from ..merchant import MerchantGateway
from ..terms import Approval, Terms, minor_units, money, utc
from . import hmac_auth
from .idempotency import IdempotencyStore, Stored, fingerprint

log = logging.getLogger("countersign.acp")

API_VERSION = "2026-04-17"
SUPPORTED_VERSIONS = (API_VERSION,)
APPROVAL_TTL = timedelta(minutes=15)
SESSION_TTL = timedelta(hours=1)
PREFIX = "/acp/checkout_sessions"


@dataclass
class AcpConfig:
    secret: bytes
    scheme: str = "airwallex"
    observe: bool = False  # log header names and body keys of refused calls, never values
    require_shopper_signature: bool = False  # U1: completion needs the shopper's own signature
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
        return cls(secret=secret.encode(), scheme=scheme, observe=flag("COUNTERSIGN_ACP_OBSERVE"),
                   require_shopper_signature=flag("COUNTERSIGN_REQUIRE_SHOPPER_SIGNATURE"),
                   default_currency=environ.get("COUNTERSIGN_ACP_CURRENCY", "USD").upper(),
                   public_url=environ.get("COUNTERSIGN_PUBLIC_URL") or None)


def to_minor(amount: Decimal, currency: str) -> int:
    """Integer minor units, or ValueError if the amount does not convert exactly."""
    scaled = Decimal(amount).scaleb(minor_units(currency))
    if scaled != scaled.to_integral_value():
        raise ValueError(f"{amount} {currency} is not a whole number of minor units")
    return int(scaled)


def from_minor(value: Any, currency: str) -> Decimal:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ValueError("amounts are non-negative integers in minor units")
    return Decimal(value).scaleb(-minor_units(currency))


class AcpError(Exception):
    def __init__(self, status: int, code: str, message: str, param: str | None = None, **extra: Any):
        super().__init__(message)
        self.status, self.code, self.message, self.param, self.extra = status, code, message, param, extra

    def body(self) -> dict[str, Any]:
        out = {"type": "invalid_request", "code": self.code, "message": self.message}
        if self.param:
            out["param"] = self.param
        out.update(self.extra)
        return out


@dataclass
class Session:
    id: str
    created: datetime
    currency: str  # shopper currency, upper case inside, lower case on the wire
    cart: dict[str, int] = field(default_factory=dict)
    ceiling_minor: int | None = None  # shopper's ceiling, if they set one above the quoted total
    buyer: dict[str, Any] | None = None
    status: str = "not_ready_for_payment"  # ACP's enum only, plus "expired" internally
    terms: Terms | None = None
    approval: Approval | None = None
    shopper_signed_hash: str | None = None  # set by U1 when the shopper signs these exact terms
    messages: list[dict[str, Any]] = field(default_factory=list)
    order: Any = None
    receipt: dict[str, Any] | None = None
    lock: threading.Lock = field(default_factory=threading.Lock, repr=False)


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
    if not isinstance(cur, str) or len(cur) != 3 or not cur.isalpha():
        raise AcpError(400, "invalid", "currency must be a three-letter code", "$.currency")
    return cur.upper()


def _amount(value: Decimal, currency: str) -> int:
    return to_minor(money(value, currency), currency)


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

    # ---------------------------------------------------------------- the shape

    def view(self, s: Session) -> dict[str, Any]:
        cur = s.currency
        out: dict[str, Any] = {
            "id": s.id,
            "status": s.status,
            "currency": cur.lower(),
            "payment_provider": {"provider": "airwallex", "supported_payment_methods": ["card"]},
            "line_items": [],
            "totals": [],
            "fulfillment_options": [],
            "messages": list(s.messages),
            "links": [],
        }
        if self.awaiting_shopper(s):
            out["links"].append({"type": "approval", "url": self.approval_link(s)})
        if s.buyer:
            out["buyer"] = s.buyer
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
                "permalink_url": f"{self.config.public_url.rstrip('/')}{PREFIX}/{s.id}/receipt"
                if self.config.public_url else None,
            }
            out.setdefault("countersign", {})["receipt"] = s.receipt
        return out

    # ---------------------------------------------------------------- the logic

    def _quote(self, s: Session, now: datetime) -> None:
        """Price the cart now and record it as what the shopper is being shown."""
        s.messages = []
        s.terms = s.approval = None
        if not s.cart:
            s.status = "not_ready_for_payment"
            return
        try:
            terms = self.merchant.offer(s.cart, s.currency, now)
        except KeyError as err:
            s.status = "not_ready_for_payment"
            s.messages = [{"type": "error", "code": "invalid", "param": "$.items",
                           "content_type": "plain", "content": f"no product with id {err.args[0]}"}]
            return
        except LookupError as err:
            s.status = "not_ready_for_payment"
            s.messages = [{"type": "error", "code": "out_of_stock", "param": "$.items",
                           "content_type": "plain", "content": str(err)}]
            return
        ceiling = terms.shopper_total
        if s.ceiling_minor is not None:
            ceiling = max(ceiling, from_minor(s.ceiling_minor, s.currency))
        s.terms = terms
        s.approval = Approval.grant(terms, ceiling, self._approver(s), now, now + APPROVAL_TTL)
        if self.awaiting_shopper(s):
            s.status = "not_ready_for_payment"
            s.messages = [{"type": "info", "code": "approval_required", "content_type": "plain",
                           "content": f"The shopper approves these exact terms here: {self.approval_link(s)}"}]
        else:
            s.status = "ready_for_payment"

    def awaiting_shopper(self, s: Session) -> bool:
        """U1 is on and the shopper has not signed the terms now on the session."""
        return (self.config.require_shopper_signature and s.approval is not None
                and s.status not in ("completed", "canceled", "expired")
                and s.shopper_signed_hash != s.approval.hash)

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
        ceiling = body.get("ceiling")  # ours, not ACP: the shopper's "up to" in minor units
        if ceiling is not None:
            try:
                from_minor(ceiling, s.currency)
            except ValueError as err:
                raise AcpError(400, "invalid", str(err), "$.ceiling") from None
            s.ceiling_minor = ceiling

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
            cart = _read_items(body)
            if cart is not None:
                s.cart = cart
            currency = _read_currency(body)
            if currency is not None:
                s.currency = currency
            self._apply_extras(s, body)
            self._quote(s, self.clock())
            return 200, self.view(s)

    def _open(self, s: Session) -> None:
        if s.status in ("completed", "canceled", "expired"):
            raise AcpError(405, "invalid_state", f"the session is {s.status}")

    def complete(self, session_id: str, body: dict[str, Any]) -> tuple[int, dict[str, Any]]:
        s = self._session(session_id)
        with s.lock:
            if s.status == "completed":
                return 200, self.view(s)  # completing twice never charges twice
            self._open(s)
            if isinstance(body.get("buyer"), dict):
                s.buyer = body["buyer"]
            if s.approval is None or s.terms is None:
                raise AcpError(400, "invalid_state", "the session has nothing ready to pay for")
            if self.awaiting_shopper(s):
                s.status = "not_ready_for_payment"
                s.messages = [{"type": "error", "code": "approval_required", "content_type": "plain",
                               "content": "The shopper has not approved these exact terms. Nothing was charged."}]
                return 200, self.view(s)

            now = self.clock()
            before = s.terms
            result = countersign(self.merchant, s.approval, f"acp:{s.id}", now,
                                 agent=None, signer=self.signer, store=self.store)
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

    def cancel(self, session_id: str) -> tuple[int, dict[str, Any]]:
        s = self._session(session_id)
        with s.lock:
            self._open(s)
            s.status, s.messages = "canceled", []
            return 200, self.view(s)


def router(api: AcpApi) -> APIRouter:
    r = APIRouter()
    cfg = api.config

    async def handle(request: Request, action: Callable[[Any], tuple[int, dict[str, Any]]], *, post: bool):
        raw = await request.body()
        reason = hmac_auth.verify(dict(request.headers), raw, cfg.secret, scheme=cfg.scheme)
        if reason is not None:
            if cfg.observe:
                observe(request, raw)
            return JSONResponse({"type": "invalid_request", "code": "unauthorized", "message": reason}, status_code=401)

        echo = {"Request-Id": request.headers["request-id"]} if "request-id" in request.headers else {}
        version = request.headers.get("api-version")
        if version is not None and version not in SUPPORTED_VERSIONS:
            return JSONResponse({"type": "invalid_request", "code": "unsupported_api_version",
                                 "message": f"API-Version {version} is not supported",
                                 "supported_versions": list(SUPPORTED_VERSIONS)}, status_code=400, headers=echo)
        try:
            body = json.loads(raw) if raw else {}
        except ValueError:
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
            return JSONResponse(err.body(), status_code=err.status, headers=echo)
        return JSONResponse(payload, status_code=status, headers=echo)

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

    return r


def observe(request: Request, raw: bytes) -> None:
    """U4: learn the caller's contract from a refused call without keeping anything sensitive.

    Logs header NAMES and top-level body KEYS only. Values can hold the secret,
    a card number or an address, so they are never logged.
    """
    try:
        parsed = json.loads(raw) if raw else {}
        keys = sorted(parsed) if isinstance(parsed, dict) else [type(parsed).__name__]
    except ValueError:
        keys = ["<not json>"]
    log.warning("acp observe: %s %s headers=%s body_keys=%s", request.method, request.url.path,
                sorted(k.lower() for k in request.headers.keys()), keys)


def is_acp(path: str) -> bool:
    return path == PREFIX or path.startswith(PREFIX + "/")
