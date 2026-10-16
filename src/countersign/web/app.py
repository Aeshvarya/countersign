"""The demo app: a shopper, their agent, and the merchant's edge on one screen.

    uvicorn countersign.web.app:app --reload        # http://127.0.0.1:8000

The agent is Claude when ANTHROPIC_API_KEY is set (COUNTERSIGN_MODEL=scripted
forces the offline stand-in). The merchant is in-process (FakeMerchant behind
a TAP-verifying edge) until the Airwallex merchant lands (WORKPLAN V5).

Each agent action runs in a background thread; the page polls the session.
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
from ..agent import AgentKey, AgentSession, ClaudeModel, LocalShop, ScriptedModel
from ..fx import FakeFx
from ..merchant import FakeMerchant

STATIC = Path(__file__).parent / "static"
AGENT_DIRECTORY = "https://agents.countersign.dev"


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
        self.shop = LocalShop(self.merchant, self.agent_key, self.clock,
                              signer=receipts.MerchantSigner(self.merchant_key, "merchant-key-1"))
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
            "edge": self.shop.edge_log[-30:],
            "receipts": s.receipts,
            "merchant_key": self.merchant_public_b64(),
            "orders": [wire.order_to_json(o) for o in self.merchant.orders.values()],
            "approvals": {k: wire.approval_to_json(a) for k, a in s.approvals.items()},
        }


app = FastAPI(title="Countersign")
WORLDS: dict[str, World] = {}


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


@app.get("/")
def index() -> FileResponse:
    return FileResponse(STATIC / "index.html")


@app.post("/api/sessions")
def start(body: Start) -> dict[str, Any]:
    if body.currency not in ("INR", "EUR"):
        raise HTTPException(400, "currency must be INR or EUR")
    w = World(body.request.strip()[:300], body.currency, body.model or os.environ.get("COUNTERSIGN_MODEL"))
    WORLDS[w.session.id] = w
    w.background(w.session.start, w.request)
    return w.view()


@app.get("/api/sessions/{session_id}")
def get(session_id: str) -> dict[str, Any]:
    return world(session_id).view()


@app.post("/api/sessions/{session_id}/approve")
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


@app.post("/api/sessions/{session_id}/decline")
def decline(session_id: str) -> dict[str, Any]:
    w = world(session_id)
    if w.session.pending is None or w.busy:
        raise HTTPException(409, "nothing is waiting for approval")
    w.background(w.session.decline)
    return w.view()


# ------------------------------------------------------------------ levers
# Each lever changes the world between approval and payment, so the merchant's
# countersign has something real to catch.


@app.post("/api/sessions/{session_id}/levers/rate")
def lever_rate(session_id: str, body: Lever) -> dict[str, Any]:
    w = world(session_id)
    w.fx.rate = Decimal(body.value or "88.40")
    w.session._event("market", f"Market moved: 1 USD = {w.fx.rate} {w.session.shopper_currency}")
    return w.view()


@app.post("/api/sessions/{session_id}/levers/delivery")
def lever_delivery(session_id: str) -> dict[str, Any]:
    w = world(session_id)
    for offer in w.session.offers.values():
        for item in offer.items:
            w.merchant.set_delivery(item.sku, "standard, 12-15 business days")
    w.session._event("market", "Merchant changed delivery to 12–15 business days")
    return w.view()


@app.post("/api/sessions/{session_id}/levers/price")
def lever_price(session_id: str) -> dict[str, Any]:
    w = world(session_id)
    for offer in w.session.offers.values():
        for item in offer.items:
            w.merchant.set_price(item.sku, (item.unit_price * Decimal("1.12")).quantize(Decimal("0.01")))
    w.session._event("market", "Merchant raised the price by 12%")
    return w.view()


@app.post("/api/sessions/{session_id}/levers/time")
def lever_time(session_id: str, body: Lever) -> dict[str, Any]:
    w = world(session_id)
    w.clock.offset += timedelta(minutes=int(body.value or 45))
    w.session._event("market", f"Clock moved forward {int(body.value or 45)} minutes")
    return w.view()


@app.post("/api/sessions/{session_id}/levers/bot")
def lever_bot(session_id: str) -> dict[str, Any]:
    """Callers hit /checkout that are not the shopper's agent, or tamper with its request."""
    w = world(session_id)
    shop = w.shop
    attempts = [
        ("unsigned bot", {}),
        ("stranger's key", tap.sign(Ed25519PrivateKey.generate(), authority=shop.authority, path="/checkout",
                                     key_id="agent-key-1", signature_agent=AGENT_DIRECTORY, tag=tap.PAY).headers()),
        ("browse signature reused to pay", tap.sign(w.agent_key.key, authority=shop.authority, path="/checkout",
                                                    key_id="agent-key-1", signature_agent=AGENT_DIRECTORY,
                                                    tag=tap.BROWSE).headers()),
    ]
    # A real agent's signed checkout, with the approval inside it swapped on the way.
    import json as _json
    signed_body = _json.dumps({"approval": "the one the shopper approved"}).encode()
    swapped = tap.sign(w.agent_key.key, authority=shop.authority, path="/checkout", key_id="agent-key-1",
                       signature_agent=AGENT_DIRECTORY, tag=tap.PAY, body=signed_body).headers()
    try:
        shop._edge("/checkout", tap.PAY, swapped, body=_json.dumps({"approval": "a bigger one"}).encode())
    except Exception as err:
        w.session._event("edge", f"Refused approval swapped in transit: {err}")
    good = w.agent_key.sign(shop.authority, "/checkout", tap.PAY)
    for name, headers in attempts:
        try:
            shop._edge("/checkout", tap.PAY, headers)
        except Exception as err:
            w.session._event("edge", f"Refused {name}: {err}")
    shop._edge("/checkout", tap.PAY, good)  # the real agent, once
    try:
        shop._edge("/checkout", tap.PAY, good)  # the same signature, replayed
    except Exception as err:
        w.session._event("edge", f"Refused replayed signature: {err}")
    return w.view()


class VerifyBody(BaseModel):
    receipt: dict[str, Any]
    approval: dict[str, Any] | None = None
    merchant_key: str


@app.post("/api/receipts/verify")
def verify_receipt(body: VerifyBody) -> dict[str, Any]:
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

    try:
        key = Ed25519PublicKey.from_public_bytes(base64.b64decode(body.merchant_key))
        approval = wire.approval_from_json(body.approval) if body.approval else None
    except Exception:
        raise HTTPException(400, "bad merchant key or approval") from None
    check = receipts.verify(body.receipt, key, approval)
    return {"ok": check.ok, "findings": list(check.findings)}
