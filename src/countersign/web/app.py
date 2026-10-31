"""The demo app: a shopper, their agent, and the merchant's edge on one screen.

    uvicorn countersign.web.app:app --reload        # http://127.0.0.1:8000

The agent is Claude when ANTHROPIC_API_KEY is set (COUNTERSIGN_MODEL=scripted
forces the offline stand-in). Each visitor gets their own merchant behind their
own copy of the real merchant edge (countersign.edge): every agent request is a
real HTTP request through the edge's TAP check, without a socket. The merchant
is FakeMerchant until the Airwallex merchant lands (WORKPLAN V5).

Each agent action runs in a background thread; the page polls the session.

`app` is the whole public host (web.host): the demo below, plus one long-lived
merchant that Airwallex calls at /acp and agents call through TAP.
"""

from __future__ import annotations

import base64
import os
import threading
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse
from pydantic import BaseModel

from .. import receipt as receipts
from .. import tap, wire
from ..agent import AgentKey, AgentSession, ClaudeModel, InProcessShop, ScriptedModel
from ..edge import create_app as create_edge
from ..fx import FakeFx
from ..merchant import FakeMerchant
from . import public
from .host import Host, house_from_env

STATIC = Path(__file__).parent / "static"
AGENT_DIRECTORY = "https://agents.countersign.dev"
MERCHANT_HOST = "shop.countersign.dev"
MAX_WORLDS = 200


class Clock:
    """Real time plus a demo offset, so the rate lock and approval can expire on cue."""

    def __init__(self) -> None:
        self.offset = timedelta(0)

    def __call__(self) -> datetime:
        return datetime.now(timezone.utc) + self.offset


class World:
    """One shopper's sandbox: merchant, market, agent, edge."""

    def __init__(self, request: str, currency: str, model_name: str | None) -> None:
        self.clock = Clock()
        self.fx = FakeFx("84.10" if currency == "INR" else "0.92")
        self.merchant = FakeMerchant(self.fx)
        self.agent_key = AgentKey(Ed25519PrivateKey.generate(), "agent-key-1", AGENT_DIRECTORY)
        self.merchant_key = Ed25519PrivateKey.generate()
        directory = tap.StaticDirectory({(AGENT_DIRECTORY, "agent-key-1"): self.agent_key.key.public_key()})
        self.edge = create_edge(self.merchant, directory, authority=MERCHANT_HOST, clock=self.clock,
                                signer=receipts.MerchantSigner(self.merchant_key, "merchant-key-1"))
        self.shop = InProcessShop(self.edge, self.agent_key, MERCHANT_HOST, currency=self.merchant.currency)
        use_claude = model_name != "scripted" and bool(os.environ.get("ANTHROPIC_API_KEY"))
        self.model_label = "Claude" if use_claude else "scripted agent (offline)"
        model = ClaudeModel() if use_claude else ScriptedModel()
        self.session = AgentSession(self.shop, model, "shopper@countersign.dev", currency, clock=self.clock)
        self.lock = threading.Lock()
        self.busy = False
        self.request = request
        self.last_signed: dict[str, str] | None = None

    def background(self, fn, *args) -> None:
        def run():
            with self.lock:
                self.busy = True
                try:
                    fn(*args)
                except Exception as err:  # surfaced on the timeline, never swallowed
                    self.session.status = "error"
                    self.session._event("error", f"{type(err).__name__}: {err}")
                finally:
                    self.busy = False

        threading.Thread(target=run, daemon=True).start()

    def merchant_public_b64(self) -> str:
        raw = self.merchant_key.public_key().public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)
        return base64.b64encode(raw).decode()

    def view(self) -> dict[str, Any]:
        s = self.session
        pending = None
        if s.pending is not None:
            from ..agent.session import offer_view

            pending = {
                "offer": offer_view(s.pending.offer_id, s.offers[s.pending.offer_id]),
                "summary": s.pending.summary,
                "suggested_ceiling": format(s.suggested_ceiling(), "f"),
            }
        return {
            "id": s.id,
            "status": "working" if self.busy else s.status,
            "model": self.model_label,
            "currency": s.shopper_currency,
            "now": self.clock().isoformat(),
            "rate": format(self.fx.rate, "f"),
            "events": s.events,
            "pending": pending,
            "edge": self.edge.state.edge_log[-30:],
            "receipts": s.receipts,
            "merchant_key": self.merchant_public_b64(),
            "orders": [wire.order_to_json(o) for o in self.merchant.orders.values()],
            "approvals": {k: wire.approval_to_json(a) for k, a in s.approvals.items()},
        }


demo = FastAPI(title="Countersign")
WORLDS: dict[str, World] = {}
HOUSE = house_from_env(os.environ)


def world(session_id: str) -> World:
    if session_id not in WORLDS:
        raise HTTPException(404, "no such session")
    return WORLDS[session_id]


class Start(BaseModel):
    request: str
    currency: str = "INR"
    model: str | None = None


class Ceiling(BaseModel):
    ceiling: str | None = None


class Lever(BaseModel):
    value: str | None = None


@demo.get("/")
def index() -> FileResponse:
    return FileResponse(STATIC / "index.html")


demo.include_router(public.router(HOUSE))


@demo.post("/api/sessions")
def start(body: Start) -> dict[str, Any]:
    if body.currency not in ("INR", "EUR"):
        raise HTTPException(400, "currency must be INR or EUR")
    # COUNTERSIGN_MODEL=scripted on the host is a spend guard: a visitor's request cannot turn Claude on.
    configured = os.environ.get("COUNTERSIGN_MODEL", "").strip().lower()
    model = "scripted" if configured == "scripted" else (body.model or configured or None)
    w = World(body.request.strip()[:300], body.currency, model)
    while len(WORLDS) >= MAX_WORLDS:  # a public demo must not grow without bound
        WORLDS.pop(next(iter(WORLDS)))
    WORLDS[w.session.id] = w
    w.background(w.session.start, w.request)
    return w.view()


@demo.get("/api/sessions/{session_id}")
def get(session_id: str) -> dict[str, Any]:
    return world(session_id).view()


@demo.post("/api/sessions/{session_id}/approve")
def approve(session_id: str, body: Ceiling) -> dict[str, Any]:
    w = world(session_id)
    if w.session.pending is None or w.busy:
        raise HTTPException(409, "nothing is waiting for approval")
    try:
        ceiling = Decimal(body.ceiling) if body.ceiling else None
    except InvalidOperation:
        raise HTTPException(400, "the ceiling must be a number") from None
    offer = w.session.offers[w.session.pending.offer_id]
    if ceiling is not None and ceiling < offer.shopper_total:
        raise HTTPException(400, "the ceiling cannot be below the price you were shown")
    w.background(w.session.approve, ceiling)
    return w.view()


@demo.post("/api/sessions/{session_id}/decline")
def decline(session_id: str) -> dict[str, Any]:
    w = world(session_id)
    if w.session.pending is None or w.busy:
        raise HTTPException(409, "nothing is waiting for approval")
    w.background(w.session.decline)
    return w.view()


# ------------------------------------------------------------------ levers
# Each lever changes the world between approval and payment, so the merchant's
# countersign has something real to catch.


@demo.post("/api/sessions/{session_id}/levers/rate")
def lever_rate(session_id: str, body: Lever) -> dict[str, Any]:
    w = world(session_id)
    w.fx.rate = Decimal(body.value or "88.40")
    w.session._event("market", f"Market moved: 1 USD = {w.fx.rate} {w.session.shopper_currency}")
    return w.view()


@demo.post("/api/sessions/{session_id}/levers/delivery")
def lever_delivery(session_id: str) -> dict[str, Any]:
    w = world(session_id)
    for offer in w.session.offers.values():
        for item in offer.items:
            w.merchant.set_delivery(item.sku, "standard, 12-15 business days")
    w.session._event("market", "Merchant changed delivery to 12–15 business days")
    return w.view()


@demo.post("/api/sessions/{session_id}/levers/price")
def lever_price(session_id: str) -> dict[str, Any]:
    w = world(session_id)
    for offer in w.session.offers.values():
        for item in offer.items:
            w.merchant.set_price(item.sku, (item.unit_price * Decimal("1.12")).quantize(Decimal("0.01")))
    w.session._event("market", "Merchant raised the price by 12%")
    return w.view()


@demo.post("/api/sessions/{session_id}/levers/time")
def lever_time(session_id: str, body: Lever) -> dict[str, Any]:
    w = world(session_id)
    w.clock.offset += timedelta(minutes=int(body.value or 45))
    w.session._event("market", f"Clock moved forward {int(body.value or 45)} minutes")
    return w.view()


@demo.post("/api/sessions/{session_id}/levers/bot")
def lever_bot(session_id: str) -> dict[str, Any]:
    """Real HTTP requests at the merchant's edge that are not the shopper's agent, or tamper with it."""
    import json

    w = world(session_id)
    client = w.shop.client
    approval = next(iter(w.session.approvals.values()), None)
    body = json.dumps({"approval": wire.approval_to_json(approval) if approval else {}, "idempotency_key": "bot-1"}).encode()
    stranger = Ed25519PrivateKey.generate()

    def signed(key, tag, data=body, path="/checkout"):
        return tap.sign(key, authority=MERCHANT_HOST, path=path, key_id="agent-key-1",
                        signature_agent=AGENT_DIRECTORY, tag=tag, body=data).headers()

    swapped = json.dumps({"approval": "a bigger one", "idempotency_key": "bot-1"}).encode()
    attempts = [
        ("unsigned bot", {}, body),
        ("stranger's key", signed(stranger, tap.PAY), body),
        ("browse signature used to pay", signed(w.agent_key.key, tap.BROWSE), body),
        ("approval swapped in transit", signed(w.agent_key.key, tap.PAY), swapped),
    ]
    for name, headers, data in attempts:
        r = client.post("/checkout", content=data, headers={**headers, "Content-Type": "application/json"})
        if r.status_code == 401:
            w.session._event("edge", f"Refused {name}: {r.json()['refused']}")
    # The real agent's signature for a harmless lookup, sent twice: the second is a replay.
    lookup = tap.sign(w.agent_key.key, authority=MERCHANT_HOST, path="/orders/bot-1", key_id="agent-key-1",
                      signature_agent=AGENT_DIRECTORY, tag=tap.PAY).headers()
    client.get("/orders/bot-1", headers=lookup)
    r = client.get("/orders/bot-1", headers=lookup)
    if r.status_code == 401:
        w.session._event("edge", f"Refused replayed signature: {r.json()['refused']}")
    return w.view()


class VerifyBody(BaseModel):
    receipt: dict[str, Any]
    approval: dict[str, Any] | None = None
    merchant_key: str


@demo.post("/api/receipts/verify")
def verify_receipt(body: VerifyBody) -> dict[str, Any]:
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

    try:
        key = Ed25519PublicKey.from_public_bytes(base64.b64decode(body.merchant_key))
        approval = wire.approval_from_json(body.approval) if body.approval else None
    except Exception:
        raise HTTPException(400, "bad merchant key or approval") from None
    check = receipts.verify(body.receipt, key, approval)
    return {"ok": check.ok, "findings": list(check.findings)}


app = Host(demo, HOUSE)
