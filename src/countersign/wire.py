"""C7: JSON on the wire between the agent and the merchant's HTTP API.

Amounts travel as strings ("127.99"), never floats, so no rounding happens in
transit. Times are ISO 8601 in UTC. from_json(to_json(x)) == x for every type here.
"""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from typing import Any

from .decide import Change, Decision, Outcome
from .merchant import Order, Product
from .terms import Approval, FxLock, LineItem, Terms, utc


def _d(value: Decimal | None) -> str | None:
    return None if value is None else format(value, "f")


def _t(value: datetime) -> str:
    return utc(value).isoformat()


def product(p: Product) -> dict[str, Any]:
    return {"sku": p.sku, "title": p.title, "category": p.category, "price": _d(p.price), "stock": p.stock, "delivery": p.delivery}


def terms_to_json(t: Terms) -> dict[str, Any]:
    return {
        "merchant_id": t.merchant_id,
        "items": [{"sku": i.sku, "title": i.title, "quantity": i.quantity, "unit_price": _d(i.unit_price)} for i in t.items],
        "delivery": t.delivery,
        "merchant_currency": t.merchant_currency,
        "merchant_total": _d(t.merchant_total),
        "shopper_currency": t.shopper_currency,
        "shopper_total": _d(t.shopper_total),
        "fx": None
        if t.fx is None
        else {
            "quote_id": t.fx.quote_id,
            "merchant_currency": t.fx.merchant_currency,
            "shopper_currency": t.fx.shopper_currency,
            "rate": _d(t.fx.rate),
            "expires_at": _t(t.fx.expires_at),
        },
    }


def terms_from_json(d: dict[str, Any]) -> Terms:
    fx = d.get("fx")
    return Terms(
        merchant_id=d["merchant_id"],
        items=tuple(LineItem(i["sku"], i["title"], int(i["quantity"]), Decimal(i["unit_price"])) for i in d["items"]),
        delivery=d["delivery"],
        merchant_currency=d["merchant_currency"],
        merchant_total=Decimal(d["merchant_total"]),
        shopper_currency=d["shopper_currency"],
        shopper_total=Decimal(d["shopper_total"]),
        fx=None
        if fx is None
        else FxLock(fx["quote_id"], fx["merchant_currency"], fx["shopper_currency"], Decimal(fx["rate"]), datetime.fromisoformat(fx["expires_at"])),
    )


def approval_to_json(a: Approval) -> dict[str, Any]:
    return {
        "terms": terms_to_json(a.terms),
        "ceiling": _d(a.ceiling),
        "approved_at": _t(a.approved_at),
        "expires_at": _t(a.expires_at),
        "approver": a.approver,
        "hash": a.hash,
    }


def approval_from_json(d: dict[str, Any]) -> Approval:
    # The hash is carried, not recomputed: decide() refuses an approval whose
    # hash does not match its own terms, which is the point of carrying it.
    return Approval(
        terms=terms_from_json(d["terms"]),
        ceiling=Decimal(d["ceiling"]),
        approved_at=datetime.fromisoformat(d["approved_at"]),
        expires_at=datetime.fromisoformat(d["expires_at"]),
        approver=d["approver"],
        hash=d["hash"],
    )


def _plain(value: Any) -> Any:
    if isinstance(value, Decimal):
        return _d(value)
    if isinstance(value, tuple):
        return [_plain(v) for v in value]
    if isinstance(value, LineItem):
        return {"sku": value.sku, "title": value.title, "quantity": value.quantity, "unit_price": _d(value.unit_price)}
    return value


def decision_to_json(dec: Decision) -> dict[str, Any]:
    return {
        "outcome": dec.outcome.value,
        "reason": dec.reason,
        "changes": [{"field": c.field, "approved": _plain(c.approved), "live": _plain(c.live)} for c in dec.changes],
        "charge": _d(dec.charge),
        "currency": dec.currency,
        "fx_quote_id": dec.fx_quote_id,
    }


def decision_from_json(d: dict[str, Any]) -> Decision:
    return Decision(
        outcome=Outcome(d["outcome"]),
        reason=d["reason"],
        changes=tuple(Change(c["field"], c["approved"], c["live"]) for c in d.get("changes", [])),
        charge=None if d.get("charge") is None else Decimal(d["charge"]),
        currency=d.get("currency"),
        fx_quote_id=d.get("fx_quote_id"),
    )


def order_to_json(o: Order) -> dict[str, Any]:
    return {
        "order_number": o.order_number,
        "payment_id": o.payment_id,
        "status": o.status,
        "amount": _d(o.amount),
        "currency": o.currency,
        "idempotency_key": o.idempotency_key,
        "approval_hash": o.approval_hash,
    }


def order_from_json(d: dict[str, Any]) -> Order:
    return Order(d["order_number"], d["payment_id"], d["status"], Decimal(d["amount"]), d["currency"],
                 d["idempotency_key"], d.get("approval_hash"))
