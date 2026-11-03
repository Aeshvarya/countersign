"""The shopper's side of one checkout, from the agent's request to the receipt.

An agent asks for a checkout (through Airwallex's merchant MCP or ours, both land
on /acp). The shopper then has one page, /approve/<id> (also /pay/<id>), that
walks them through what is left for them:

    approve   U1 is on and the current terms wait for their passkey
    waiting   approved (or U1 is off); the agent has not completed yet
    pay       the order exists and its card payment is open in Airwallex's Drop-in
    paid      the payment is confirmed; the receipt follows once it is signed
    failed    the payment was cancelled, or refunded because it did not match
    canceled  the session was canceled or expired; nothing was charged

GET  /api/checkout/<id>           where the checkout stands, as the page needs it
POST /api/checkout/<id>/confirm   after Drop-in says "success", ask the merchant to
                                  check the payment with Airwallex now instead of
                                  waiting for the webhook. It fetches the intent
                                  itself (AirwallexMerchant.confirm); what the
                                  browser says is never trusted.

The session id in the address is the capability, as on the approval routes. The
card itself only ever goes into Airwallex's Drop-in: the page receives the
intent's client secret, which is made for the browser and can only pay this
intent.
"""

from __future__ import annotations

import threading
import time
from typing import Any, Callable

from fastapi import APIRouter
from fastapi.responses import JSONResponse
from starlette.concurrency import run_in_threadpool

from ..airwallex import UNREACHABLE, AirwallexError
from ..checkout import release
from ..edge.acp import AcpError, to_minor
from ..terms import money, utc
from .host import record_payment

CONFIRM_EVERY = 3.0  # seconds between Airwallex lookups for one session


def drop_in_env(merchant: Any) -> str:
    """Drop-in's `env`: demo for the sandbox, prod otherwise."""
    base = getattr(getattr(merchant, "client", None), "base_url", "") or ""
    return "prod" if base and "sandbox" not in base and "demo" not in base else "demo"


class Shopper:
    def __init__(self, house: Any, clock: Callable[[], float] = time.monotonic) -> None:
        self.house = house
        self.clock = clock
        self._last_confirm: dict[str, float] = {}
        self._lock = threading.Lock()

    @property
    def api(self):
        return getattr(self.house.edge.state, "acp", None)

    def _session(self, session_id: str):
        api = self.api
        if api is None:
            raise AcpError(404, "not_found", "checkout is not enabled on this host")
        return api, api._session(session_id)  # raises 404, and marks a session past its TTL expired

    def state(self, session_id: str) -> dict[str, Any]:
        api, s = self._session(session_id)
        merchant = self.house.merchant
        key = f"acp:{s.id}"
        with s.lock:
            view = api.view(s)
            awaiting = api.awaiting_shopper(s)
            signed = s.approval is not None and s.shopper_signed == api.challenge(s)
            terms = self._terms(s)
        order = merchant.find_order(key)
        receipt = self.house.store.get(key)
        problems: list[str] = []
        payment = None
        if order is None:
            stage = "canceled" if view["status"] == "canceled" else "approve" if awaiting else "waiting"
        elif order.status == "pending":
            handoff = merchant.handoff(key) if hasattr(merchant, "handoff") else None
            stage = "pay" if handoff is not None else "waiting"
            if handoff is not None:
                payment = {**handoff.as_json(), "env": drop_in_env(merchant)}
        elif order.status == "paid":
            stage = "paid"
        else:
            stage = "failed"
            problems = list(getattr(merchant, "problems", {}).get(key, ())) or [f"the payment is {order.status}"]
        return {
            "session_id": s.id,
            "stage": stage,
            "status": view["status"],
            "signed": signed,
            "approval_required": api.config.require_shopper_signature,
            "messages": [m.get("content") for m in view["messages"] if m.get("content")],
            "changed": changed_since_shown(view["messages"]),
            "terms": terms,
            "order": None if order is None else {
                "order_number": order.order_number, "status": order.status,
                "amount": None if order.amount is None else format(order.amount, "f"),
                "currency": order.currency,
            },
            "payment": payment,
            "receipt": receipt,
            "merchant_key": self.house.public_key(),
            "problems": problems,
        }

    @staticmethod
    def _terms(s) -> dict[str, Any] | None:
        t, a = s.terms, s.approval
        if t is None:
            return None
        cur = s.currency
        return {
            "merchant_id": t.merchant_id,
            "items": [{"id": i.sku, "title": i.title, "quantity": i.quantity} for i in t.items],
            "delivery": t.delivery,
            "currency": cur.lower(),
            "total": to_minor(money(t.shopper_total, cur), cur),
            "ceiling": to_minor(money(a.ceiling, cur), cur) if a is not None else None,
            "merchant_currency": t.merchant_currency.lower(),
            "merchant_total": to_minor(money(t.merchant_total, t.merchant_currency), t.merchant_currency),
            "rate": format(t.fx.rate, "f") if t.fx else None,
            "terms_hash": a.hash if a is not None else None,
            "expires_at": utc(a.expires_at).isoformat() if a is not None else None,
        }

    def confirm(self, session_id: str) -> tuple[int, dict[str, Any]]:
        """Check the session's payment with Airwallex now. Throttled per session; safe to repeat."""
        _, s = self._session(session_id)
        key = f"acp:{s.id}"
        merchant = self.house.merchant
        order = merchant.find_order(key)
        if order is not None and order.status == "pending" and hasattr(merchant, "confirm"):
            with self._lock:
                now = self.clock()
                if now - self._last_confirm.get(key, float("-inf")) < CONFIRM_EVERY:
                    return 429, {"type": "invalid_request", "code": "too_soon",
                                 "message": "the payment is being checked; try again in a few seconds"}
                self._last_confirm[key] = now
            try:
                order = merchant.confirm(order.payment_id)
            except (AirwallexError, *UNREACHABLE):
                return 503, {"type": "service_unavailable", "code": "airwallex_unreachable",
                             "message": "Airwallex could not be reached; the payment webhook will settle it"}
            receipt = release(order, self.house.signer, self.house.store)  # signs the held receipt once paid
            record_payment(self.house, order, receipt, "checked from the shopper's page")
        elif order is not None:
            release(order, self.house.signer, self.house.store)
        return 200, self.state(session_id)


def changed_since_shown(messages: list[dict[str, Any]]) -> dict[str, Any] | None:
    """Why the shopper is asked again, from the session's own re-ask messages (C10), or None.

    previous_total is set when the total moved: the page shows the old price
    struck through beside the new one, in the same minor units as the terms.
    """
    reasons = [m["content"] for m in messages
               if m.get("resolution") == "requires_buyer_review" and isinstance(m.get("content"), str)]
    if not reasons:
        return None
    moved = next((m for m in messages if m.get("code") == "price_change"), None)
    previous = moved.get("previous_amount") if moved else None
    return {"previous_total": previous if isinstance(previous, int) else None, "reasons": reasons}


def router(house: Any, shopper: Shopper | None = None) -> APIRouter:
    shopper = shopper or Shopper(house)
    r = APIRouter()
    no_store = {"Cache-Control": "no-store"}

    async def run(action: Callable[[], tuple[int, dict[str, Any]]]) -> JSONResponse:
        try:
            status, body = await run_in_threadpool(action)
        except AcpError as err:
            return JSONResponse(err.body(), status_code=err.status, headers=no_store)
        return JSONResponse(body, status_code=status, headers=no_store)

    @r.get("/api/checkout/{session_id}")
    async def checkout_state(session_id: str):
        return await run(lambda: (200, shopper.state(session_id)))

    @r.post("/api/checkout/{session_id}/confirm")
    async def checkout_confirm(session_id: str):
        return await run(lambda: shopper.confirm(session_id))

    return r
