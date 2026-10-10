"""One shopping conversation: the tool loop, the approval pause, the timeline.

The loop runs until the model finishes, or until it asks the shopper to approve.
Then it stops and waits: `approve(ceiling)` or `decline()` resumes it. Every
number the shopper sees on the approval screen comes from the offer the code
stored, never from the model's text.
"""

from __future__ import annotations

import json
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from typing import Any, Callable

from ..decide import Outcome
from ..terms import Approval, Terms, money
from .model import Model
from .shop import Refused, ShopClient, new_idempotency_key

SYSTEM = """You are a shopping agent buying on behalf of one shopper, from one merchant.

How you work:
- Find what the shopper asked for with search_products, then get exact terms with get_offer.
- Before any purchase, call request_approval with the offer_id and a one-sentence summary. The shopper sees the exact terms from the merchant next to your summary and sets the most they will pay in their own currency.
- Only call checkout for an offer the shopper approved.
- The merchant countersigns every checkout. If it answers "reask", tell the shopper plainly what changed, in their currency, call get_offer for fresh terms and ask again. If it answers "refuse", stop and say why; never retry a refused checkout.

Rules:
- Never state a price, total, rate or date that did not come from a tool result.
- Never invent a SKU. Use only SKUs from search results.
- Keep messages to the shopper short: one or two sentences."""

TOOLS: list[dict[str, Any]] = [
    {
        "name": "search_products",
        "description": "Search the merchant's catalogue. Returns matching in-stock products with price (merchant currency), stock and delivery.",
        "strict": True,
        "input_schema": {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "Plain words, e.g. 'mechanical keyboard'"},
                "limit": {"type": "integer", "description": "How many results, 1 to 10"},
            },
            "required": ["query", "limit"],
            "additionalProperties": False,
        },
    },
    {
        "name": "get_offer",
        "description": "Ask the merchant for exact, current terms for a cart: items, delivery, totals in both currencies and the locked exchange rate. Returns an offer_id.",
        "strict": True,
        "input_schema": {
            "type": "object",
            "properties": {
                "items": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {"sku": {"type": "string"}, "quantity": {"type": "integer"}},
                        "required": ["sku", "quantity"],
                        "additionalProperties": False,
                    },
                }
            },
            "required": ["items"],
            "additionalProperties": False,
        },
    },
    {
        "name": "request_approval",
        "description": "Show the shopper the exact terms of an offer and wait for their decision. They set the most they will pay, in their currency.",
        "strict": True,
        "input_schema": {
            "type": "object",
            "properties": {
                "offer_id": {"type": "string"},
                "summary": {"type": "string", "description": "One sentence on why this is the right buy."},
            },
            "required": ["offer_id", "summary"],
            "additionalProperties": False,
        },
    },
    {
        "name": "checkout",
        "description": "Pay for an approved offer. The merchant re-checks the live terms against the approval and answers proceed, reask or refuse.",
        "strict": True,
        "input_schema": {
            "type": "object",
            "properties": {"offer_id": {"type": "string"}},
            "required": ["offer_id"],
            "additionalProperties": False,
        },
    },
]


def _d(value: Decimal | None) -> str | None:
    return None if value is None else format(value, "f")


def offer_view(offer_id: str, t: Terms) -> dict[str, Any]:
    return {
        "offer_id": offer_id,
        "merchant_id": t.merchant_id,
        "items": [{"sku": i.sku, "title": i.title, "quantity": i.quantity, "unit_price": _d(i.unit_price)} for i in t.items],
        "delivery": t.delivery,
        "merchant_total": _d(t.merchant_total),
        "merchant_currency": t.merchant_currency,
        "shopper_total": _d(t.shopper_total),
        "shopper_currency": t.shopper_currency,
        "rate": _d(t.fx.rate) if t.fx else None,
        "rate_locked_until": t.fx.expires_at.isoformat() if t.fx else None,
    }


@dataclass
class Pending:
    tool_use_id: str
    offer_id: str
    summary: str


@dataclass
class AgentSession:
    shop: ShopClient
    model: Model
    shopper: str
    shopper_currency: str
    clock: Callable[[], datetime] = lambda: datetime.now(timezone.utc)
    approval_ttl: timedelta = timedelta(minutes=30)
    id: str = field(default_factory=lambda: uuid.uuid4().hex[:12])
    status: str = "idle"  # idle | working | awaiting_approval | done | error
    messages: list[dict[str, Any]] = field(default_factory=list)
    events: list[dict[str, Any]] = field(default_factory=list)
    offers: dict[str, Terms] = field(default_factory=dict)
    approvals: dict[str, Approval] = field(default_factory=dict)
    keys: dict[str, str] = field(default_factory=dict)
    receipts: list[dict[str, Any]] = field(default_factory=list)
    pending: Pending | None = None
    _held: list[dict[str, Any]] = field(default_factory=list)

    # ------------------------------------------------------------ timeline

    def _event(self, kind: str, text: str, **data: Any) -> None:
        self.events.append({"kind": kind, "text": text, "data": data, "at": self.clock().isoformat()})

    # --------------------------------------------------------------- tools

    def _search(self, args: dict) -> dict:
        limit = max(1, min(int(args["limit"]), 10))
        products = self.shop.search(args["query"], limit)
        found = [{"sku": p.sku, "title": p.title, "category": p.category, "price": _d(p.price),
                  "currency": getattr(self.shop, "currency", None),
                  "stock": p.stock, "delivery": p.delivery} for p in products]
        self._event("tool", f"Searched “{args['query']}”: {len(found)} found", products=found)
        return {"products": found}

    def _offer(self, args: dict) -> dict:
        cart: dict[str, int] = {}
        for item in args["items"]:
            if int(item["quantity"]) < 1:
                return {"error": "quantity must be at least 1"}
            cart[item["sku"]] = cart.get(item["sku"], 0) + int(item["quantity"])
        try:
            terms = self.shop.offer(cart, self.shopper_currency)
        except (KeyError, LookupError) as err:
            self._event("tool", f"No offer: {err}")
            return {"error": str(err)}
        offer_id = f"off_{len(self.offers) + 1}"
        self.offers[offer_id] = terms
        view = offer_view(offer_id, terms)
        self._event("offer", f"Offer {offer_id}: {view['shopper_total']} {view['shopper_currency']}", offer=view)
        return view

    def _checkout(self, args: dict) -> dict:
        offer_id = args["offer_id"]
        approval = self.approvals.get(offer_id)
        if approval is None:
            return {"outcome": "refuse", "reason": "no approval for this offer; call request_approval first"}
        key = self.keys.setdefault(offer_id, new_idempotency_key())
        try:
            result = self.shop.checkout(approval, key)
        except Refused as err:
            self._event("edge", f"Merchant edge refused the request: {err}")
            return {"outcome": "refuse", "reason": f"merchant edge refused: {err}"}
        d = result.decision
        out: dict[str, Any] = {
            "outcome": d.outcome.value,
            "reason": d.reason,
            "changes": [{"field": c.field, "approved": _plain(c.approved), "live": _plain(c.live)} for c in d.changes],
            "cart": [{"sku": i.sku, "quantity": i.quantity} for i in approval.terms.items],
        }
        if d.outcome is Outcome.PROCEED and result.order:
            out.update(order_number=result.order.order_number, charged=f"{_d(d.charge)} {d.currency}",
                       receipt_saved=result.receipt is not None)
            if result.receipt:
                self.receipts.append(result.receipt)
        self._event("decision", f"Merchant countersign: {d.outcome.value.upper()} — {d.reason}", decision=out,
                    receipt=result.receipt)
        return out

    # ---------------------------------------------------------------- loop

    def start(self, request: str) -> None:
        self.messages.append({"role": "user", "content": request})
        self._event("shopper", request)
        self.run()

    def run(self, max_steps: int = 16) -> None:
        self.status = "working"
        for _ in range(max_steps):
            response = self.model.respond(SYSTEM, TOOLS, self.messages)
            for block in response.content:
                if block.type == "text" and block.text.strip():
                    self._event("agent", block.text.strip())
            self.messages.append({"role": "assistant", "content": response.content})

            if response.stop_reason == "refusal":
                self.status = "error"
                self._event("error", "The model declined this request.")
                return
            if response.stop_reason != "tool_use":
                self.status = "done"
                return

            results: list[dict[str, Any]] = []
            for block in response.content:
                if block.type != "tool_use":
                    continue
                if block.name == "request_approval":
                    offer_id = block.input["offer_id"]
                    if offer_id not in self.offers or self.pending is not None:
                        results.append(_result(block.id, {"error": "unknown offer_id"}))
                        continue
                    self.pending = Pending(block.id, offer_id, block.input["summary"])
                    view = offer_view(offer_id, self.offers[offer_id])
                    self._event("approval", "Waiting for the shopper to approve", offer=view, summary=block.input["summary"])
                    continue
                handler = {"search_products": self._search, "get_offer": self._offer, "checkout": self._checkout}.get(block.name)
                try:
                    payload = handler(block.input) if handler else {"error": f"unknown tool {block.name}"}
                except Refused as err:
                    self._event("edge", f"Merchant edge refused the request: {err}")
                    payload = {"error": f"merchant edge refused: {err}"}
                results.append(_result(block.id, payload))

            if self.pending is not None:
                self._held = results
                self.status = "awaiting_approval"
                return
            self.messages.append({"role": "user", "content": results})
        self.status = "error"
        self._event("error", "Stopped: too many steps without finishing.")

    # ------------------------------------------------------------ shopper

    def suggested_ceiling(self) -> Decimal | None:
        if self.pending is None:
            return None
        t = self.offers[self.pending.offer_id]
        return money((t.shopper_total * Decimal("1.02")).to_integral_value(rounding="ROUND_CEILING"), t.shopper_currency)

    def approve(self, ceiling: Decimal | str | None = None) -> None:
        if self.pending is None:
            raise RuntimeError("nothing is waiting for approval")
        p = self.pending
        terms = self.offers[p.offer_id]
        ceiling = Decimal(ceiling) if ceiling is not None else terms.shopper_total
        now = self.clock()
        approval = Approval.grant(terms, ceiling, self.shopper, now, now + self.approval_ttl)
        self.approvals[p.offer_id] = approval
        self._event("approved", f"Shopper approved up to {_d(approval.ceiling)} {terms.shopper_currency}",
                    hash=approval.hash, ceiling=_d(approval.ceiling))
        self._resume(_result(p.tool_use_id, {"approved": True, "offer_id": p.offer_id,
                                             "ceiling": _d(approval.ceiling), "currency": terms.shopper_currency}))

    def decline(self, reason: str = "the shopper said no") -> None:
        if self.pending is None:
            raise RuntimeError("nothing is waiting for approval")
        p = self.pending
        self._event("declined", f"Shopper declined: {reason}")
        self._resume(_result(p.tool_use_id, {"approved": False, "offer_id": p.offer_id, "reason": reason}))

    def _resume(self, approval_result: dict[str, Any]) -> None:
        self.pending = None
        self.messages.append({"role": "user", "content": [*self._held, approval_result]})
        self._held = []
        self.run()


def _plain(value: Any) -> Any:
    if isinstance(value, Decimal):
        return _d(value)
    if isinstance(value, tuple):
        return [_plain(v) for v in value]
    if hasattr(value, "sku"):
        return {"sku": value.sku, "quantity": value.quantity, "unit_price": _d(value.unit_price)}
    return value


def _result(tool_use_id: str, payload: dict[str, Any]) -> dict[str, Any]:
    return {"type": "tool_result", "tool_use_id": tool_use_id, "content": json.dumps(payload)}
