"""A15: the merchant console. What the merchant sees while agents shop: every checkout
call and the countersign's answer, passkey approvals, TAP refusals, payments,
orders and receipts, and the levers (V6) that change the catalogue on cue.

    /console                    the page (static; the token travels in its #fragment)
    GET  /api/console/feed      events since a sequence number
    GET  /api/console/state     configuration, open checkouts, orders, receipts
    GET  /api/console/products  find a product to pull a lever on
    POST /api/levers/...        price, stock, delivery (web.levers), for the page or a script

All of it sits behind COUNTERSIGN_LEVER_TOKEN (at least 32 characters) as a
bearer token, and none of it exists without one.

The feed is recorded by FeedMiddleware around the merchant's edge, so it sees
Airwallex's calls and our fallback MCP's alike. It reads the response status and
our own response JSON only. Request bodies are never read: under managed_payment
one may carry payment data.
"""

from __future__ import annotations

import hmac
import json
import logging
import re
import threading
import time
from collections import deque
from datetime import datetime, timezone
from typing import Any, Callable

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import JSONResponse
from starlette.concurrency import run_in_threadpool

from ..edge import acp as acp_api
from . import levers

log = logging.getLogger("countersign.console")

FEED_SIZE = 400
BODY_CAP = 64 * 1024  # enough to summarise any response we send
DEDUPE_SECONDS = 20
MCP_DOOR = b"x-countersign-door"  # set by our fallback MCP's caller, so the feed can say who called
TAP_PATHS = ("/products", "/offers", "/checkout")
_SESSION = re.compile(r"^/acp/(?:checkout_sessions|approvals)/(cs_[A-Za-z0-9]+)")


class Feed:
    """The last FEED_SIZE events, numbered. Thread-safe. A repeat of the same event soon after is counted, not added."""

    def __init__(self, size: int = FEED_SIZE, clock: Callable[[], datetime] = lambda: datetime.now(timezone.utc)):
        self._events: deque[dict[str, Any]] = deque(maxlen=size)
        self._seq = 0
        self._lock = threading.Lock()
        self.clock = clock

    @property
    def seq(self) -> int:
        return self._seq

    def add(self, kind: str, level: str, title: str, detail: str = "", **extra: Any) -> dict[str, Any]:
        now = self.clock()
        with self._lock:
            last = self._events[-1] if self._events else None
            if (last is not None and (last["kind"], last["title"], last.get("session"), last["detail"])
                    == (kind, title, extra.get("session"), detail)
                    and (now - datetime.fromisoformat(last["at"])).total_seconds() < DEDUPE_SECONDS):
                self._seq += 1
                last.update(seq=self._seq, at=now.isoformat(), count=last.get("count", 1) + 1)
                return last
            self._seq += 1
            event = {"id": self._seq, "seq": self._seq, "at": now.isoformat(), "kind": kind, "level": level, "title": title,
                     "detail": detail, **{k: v for k, v in extra.items() if v is not None}}
            self._events.append(event)
            return event

    def since(self, seq: int) -> list[dict[str, Any]]:
        with self._lock:
            return [dict(e) for e in self._events if e["seq"] > seq]


# ------------------------------------------------------------------ recording


def _money(minor: Any, currency: str | None) -> str:
    try:
        return f"{acp_api.from_minor(minor, currency or 'USD')} {(currency or '').upper()}"
    except (ValueError, TypeError):
        return "?"


def _total(view: dict[str, Any]) -> str:
    total = next((t.get("amount") for t in view.get("totals") or [] if t.get("type") == "total"), None)
    return _money(total, view.get("currency")) if total is not None else ""


REASK_TITLES = {  # in priority order: why the shopper is asked again, before "they still have to sign"
    "out_of_stock": "Asked the shopper again: out of stock",
    "price_change": "Asked the shopper again: the price changed",
    "expired": "Asked the shopper again: the approval expired",
    "terms_changed": "Asked the shopper again: the terms changed",
    "approval_required": "Waiting for the shopper's passkey",
}


def summarize(method: str, path: str, status: int, body: Any, door: str) -> tuple[str, str, str, dict[str, Any]] | None:
    """(kind, level, title, extra) for one response, or None if it is not worth a line."""
    data = body if isinstance(body, dict) else {}
    m = _SESSION.match(path)
    extra: dict[str, Any] = {"session": m.group(1) if m else data.get("id"), "door": door}
    message = data.get("message") or data.get("refused") or ""

    if path.startswith(acp_api.APPROVALS + "/"):
        if method == "GET" and status == 200:
            return "passkey", "info", "Shopper opened the approval page", {**extra, "detail": ""}
        if method == "POST" and status == 200:
            how = "user verified" if data.get("user_verified") else "user present"
            return "passkey", "go", "Shopper approved the exact terms with their passkey", {**extra, "detail": how}
        if method == "POST":
            return "passkey", "no", "Passkey signature refused", {**extra, "detail": message}
        return None

    if path == acp_api.PREFIX or path.startswith(acp_api.PREFIX + "/"):
        if status == 401:
            return "acp", "no", "Refused a checkout call: bad platform signature", {**extra, "detail": message}
        if status >= 400:
            return "acp", "no", f"Checkout call refused ({status})", {**extra, "detail": message}
        codes = [msg.get("code") for msg in data.get("messages") or []]
        detail = " · ".join(x for x in (_total(data), data.get("status")) if x)
        if method == "GET":
            return None  # reads are frequent and change nothing
        if path == acp_api.PREFIX:
            n = sum(li.get("item", {}).get("quantity", 0) for li in data.get("line_items") or [])
            return "acp", "info", f"Checkout created: {n} item{'s' if n != 1 else ''}", {**extra, "detail": detail}
        if path.endswith("/complete"):
            if data.get("status") == "completed":
                order = (data.get("order") or {}).get("id")
                return "acp", "go", "Countersigned: the terms still match, order placed", \
                    {**extra, "detail": f"order {order} · {_total(data)}", "order": order}
            for code in REASK_TITLES:  # the most telling reason first
                if code in codes:
                    level = "ask" if code != "approval_required" else "info"
                    content = next((msg.get("content") for msg in data.get("messages") or []
                                    if msg.get("code") == code and code != "approval_required"), None)
                    return "acp", level, REASK_TITLES[code] + "; nothing charged", \
                        {**extra, "detail": content or detail}
            return "acp", "no", "Refused at checkout; nothing charged", \
                {**extra, "detail": "; ".join(msg.get("content", "") for msg in data.get("messages") or [])}
        if path.endswith("/cancel"):
            return "acp", "info", "Checkout canceled", {**extra, "detail": ""}
        return "acp", "info", "Checkout updated", {**extra, "detail": detail}

    if path in TAP_PATHS or path.startswith("/orders/"):
        if status == 401:
            return "tap", "no", f"TAP refused a request to {path}", {**extra, "detail": message}
        if path == "/checkout" and status == 200:
            d = data.get("decision") or {}
            outcome = d.get("outcome")
            level = {"proceed": "go", "reask": "ask"}.get(outcome, "no")
            order = (data.get("order") or {}).get("order_number")
            return "tap", level, f"TAP-verified agent checkout: {outcome}", \
                {**extra, "detail": d.get("reason", ""), "order": order}
        if path == "/offers" and status == 200:
            return "tap", "info", "TAP-verified agent asked for an offer", {**extra, "detail": ""}
    return None


class FeedMiddleware:
    """ASGI middleware for the merchant's edge: one feed line per interesting response."""

    def __init__(self, app: Any, feed: Feed) -> None:
        self.app = app
        self.feed = feed

    async def __call__(self, scope, receive, send) -> None:
        path = scope.get("path", "")
        if scope["type"] != "http" or not (path.startswith("/acp/") or path in TAP_PATHS or path.startswith("/orders/")):
            await self.app(scope, receive, send)
            return
        status, parts, size = 0, [], 0

        async def capture(message):
            nonlocal status, size
            if message["type"] == "http.response.start":
                status = message["status"]
            elif message["type"] == "http.response.body" and size < BODY_CAP:
                chunk = message.get("body", b"")[: BODY_CAP - size]
                parts.append(chunk)
                size += len(chunk)
            await send(message)

        await self.app(scope, receive, capture)
        try:
            headers = dict(scope.get("headers") or [])
            door = "our MCP" if headers.get(MCP_DOOR) == b"mcp" else (
                "TAP" if not path.startswith("/acp/") else
                "shopper" if path.startswith(acp_api.APPROVALS) else "platform")
            try:
                body = json.loads(b"".join(parts)) if parts else None
            except ValueError:
                body = None
            out = summarize(scope["method"], path, status, body, door)
            if out is not None:
                kind, level, title, extra = out
                self.feed.add(kind, level, title, extra.pop("detail", ""), **extra)
        except Exception:  # the feed must never break a checkout
            log.exception("console feed could not record %s %s", scope.get("method"), path)


# ------------------------------------------------------------------ the API


def _authorized(request: Request, token: bytes) -> bool:
    auth = request.headers.get("authorization", "")
    given = auth[7:].strip().encode() if auth[:7].lower() == "bearer " else b""
    return bool(given) and hmac.compare_digest(given, token)


def state(house: Any) -> dict[str, Any]:
    api = getattr(house.edge.state, "acp", None)
    merchant = house.merchant
    sessions = []
    if api is not None:
        for s in list(api.sessions.values())[-25:][::-1]:
            t = s.terms
            sessions.append({
                "id": s.id, "status": "canceled" if s.status == "expired" else s.status,
                "currency": s.currency, "created": s.created.isoformat(),
                "items": [{"id": i.sku, "title": i.title, "quantity": i.quantity} for i in t.items] if t else [],
                "total": f"{t.shopper_total} {t.shopper_currency}" if t else None,
                "awaiting_shopper": api.awaiting_shopper(s),
                "order": s.order.order_number if s.order is not None else None,
            })
    orders = []
    for key, o in list(getattr(merchant, "orders", {}).items())[-25:][::-1]:
        orders.append({"key": key, "order_number": o.order_number, "status": o.status,
                       "amount": None if o.amount is None else format(o.amount, "f"), "currency": o.currency,
                       "payment_id": o.payment_id, "receipt": house.store.get(key)})
    tap = house.edge.state.edge_log
    return {
        "config": {
            "acp": house.acp,
            "shopper_passkey": bool(api and api.config.require_shopper_signature),
            "merchant": "airwallex" if hasattr(merchant, "confirm") else "offline (pays instantly)",
            "fallback_mcp": bool(house.mcp_token),
            "catalogue_sync": bool(getattr(house, "sync_catalog", False)),
            "public_url": house.public_url,
            "currency": merchant.currency,
        },
        "sessions": sessions,
        "orders": orders,
        "tap": {"verified": sum(1 for e in tap if e.get("ok")), "refused": sum(1 for e in tap if not e.get("ok"))},
        "merchant_key": house.public_key(),
    }


def products(house: Any, q: str, limit: int = 12) -> list[dict[str, Any]]:
    """Catalogue search for the levers: out-of-stock products too, unlike the shop's own search."""
    catalog = getattr(house.merchant, "catalog", {})
    words = q.lower().split()
    found = [p for p in catalog.values() if all(w in f"{p.sku} {p.title} {p.category}".lower() for w in words)]
    return [{"id": p.sku, "title": p.title, "category": p.category, "price": format(p.price, "f"),
             "currency": house.merchant.currency, "stock": p.stock, "delivery": p.delivery}
            for p in found[:limit]]


def router(house: Any) -> APIRouter:
    token = house.lever_token
    r = APIRouter()
    no_store = {"Cache-Control": "no-store"}

    def guard(request: Request) -> None:
        if not _authorized(request, token):
            raise HTTPException(401, "a valid bearer token is required", headers={"WWW-Authenticate": "Bearer"})

    @r.get("/api/console/feed")
    def feed(request: Request, since: int = 0):
        guard(request)
        return JSONResponse({"events": house.feed.since(since), "seq": house.feed.seq}, headers=no_store)

    @r.get("/api/console/state")
    def console_state(request: Request):
        guard(request)
        return JSONResponse(state(house), headers=no_store)

    @r.get("/api/console/products")
    def console_products(request: Request, q: str = ""):
        guard(request)
        return JSONResponse({"products": products(house, q[:80])}, headers=no_store)

    async def lever(request: Request, apply: Callable[[dict[str, Any]], levers.Change]) -> JSONResponse:
        guard(request)
        try:
            body = await request.json()
        except ValueError:
            return JSONResponse({"error": "the body is not JSON"}, status_code=400)
        if not isinstance(body, dict):
            return JSONResponse({"error": "the body must be a JSON object"}, status_code=400)
        try:
            change = await run_in_threadpool(apply, body)
        except levers.LeverError as err:
            return JSONResponse({"error": str(err)}, status_code=400)
        return JSONResponse(change.as_json(), headers=no_store)

    @r.post("/api/levers/price")
    async def lever_price(request: Request):
        def apply(b):
            if "percent" in b:
                return levers.raise_price(house, b.get("sku"), b["percent"])
            return levers.set_price(house, b.get("sku"), b.get("price"))
        return await lever(request, apply)

    @r.post("/api/levers/stock")
    async def lever_stock(request: Request):
        return await lever(request, lambda b: levers.set_stock(house, b.get("sku"), b.get("stock")))

    @r.post("/api/levers/delivery")
    async def lever_delivery(request: Request):
        return await lever(request, lambda b: levers.set_delivery(house, b.get("sku"), b.get("delivery")))

    return r
