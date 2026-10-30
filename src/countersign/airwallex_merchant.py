"""V5′: C3 against Airwallex. The catalogue we upload, real rate locks, real card payments (C9).

Search and offers read the same catalogue the GMC upload sends (C11), priced
with Airwallex conversion quotes. Paying is where it differs from FakeMerchant:

1. pay() creates the payment intent (merchant currency, order number and
   approval hash inside, the idempotency key as Airwallex's request_id) and
   returns a *pending* order. Stock is held for it. The shopper's browser
   finishes the payment in Drop-in with handoff(); no card ever reaches us.
2. Airwallex sends payment_intent.succeeded. confirm() never trusts the event:
   it fetches the intent and runs check_paid against what the countersign
   decided. Every field matches → paid. Anything else after money moved →
   refunded in full, stock released, no receipt.
3. The caller then signs the held receipt with checkout.release().

find_order() sees pending orders too, so a retry while the shopper is still
in Drop-in never starts a second intent.
"""

from __future__ import annotations

import logging
import threading
from dataclasses import replace
from typing import Any

from .airwallex import Airwallex
from .decide import Decision, Outcome
from .fx import AirwallexFx, FxGateway
from .merchant import FakeMerchant, Order
from .payments import SUCCEEDED, AirwallexPayments, Handoff, check_paid
from .terms import Terms

log = logging.getLogger("countersign.airwallex_merchant")

# Intent statuses after which no money will move.
CANCELLED = ("CANCELLED",)


def intent_of(event: dict[str, Any]) -> str | None:
    """The payment intent a webhook event is about, or None if it is about something else."""
    if not str(event.get("name", "")).startswith("payment_intent."):
        return None
    obj = (event.get("data") or {}).get("object") or {}
    intent_id = obj.get("id")
    return intent_id if isinstance(intent_id, str) and intent_id else None


class AirwallexMerchant(FakeMerchant):
    def __init__(self, client: Airwallex, fx: FxGateway | None = None, merchant_id: str = "merchant_demo",
                 currency: str = "USD") -> None:
        super().__init__(fx if fx is not None else AirwallexFx(client), merchant_id, currency)
        self.client = client
        self.payments = AirwallexPayments(client)
        self._decisions: dict[str, tuple[Decision, Terms]] = {}
        self._handoffs: dict[str, Handoff] = {}
        self._by_intent: dict[str, str] = {}
        self.problems: dict[str, tuple[str, ...]] = {}  # why a payment was refunded, by idempotency key
        self._lock = threading.Lock()

    def pay(self, terms: Terms, decision: Decision, idempotency_key: str, approval_hash: str | None = None) -> Order:
        if decision.outcome is not Outcome.PROCEED:
            raise PermissionError(f"refusing to charge: {decision.reason}")
        with self._lock:
            existing = self.orders.get(idempotency_key)
            if existing is not None:
                return existing
            order_number = f"ORD-{len(self.orders) + 1001}"
            handoff = self.payments.start(decision, merchant_total=terms.merchant_total,
                                          merchant_currency=terms.merchant_currency, order_number=order_number,
                                          approval_hash=approval_hash or "", idempotency_key=idempotency_key)
            for item in terms.items:  # held for this order; released if the payment never lands
                self.set_stock(item.sku, self.catalog[item.sku].stock - item.quantity)
            order = Order(order_number=order_number, payment_id=handoff.intent_id, status="pending",
                          amount=decision.charge, currency=decision.currency,
                          idempotency_key=idempotency_key, approval_hash=approval_hash)
            self.orders[idempotency_key] = order
            self._decisions[idempotency_key] = (decision, terms)
            self._handoffs[idempotency_key] = handoff
            self._by_intent[handoff.intent_id] = idempotency_key
            return order

    def handoff(self, idempotency_key: str) -> Handoff | None:
        """What the shopper's browser needs to mount Drop-in, while the order is pending."""
        order = self.orders.get(idempotency_key)
        return self._handoffs.get(idempotency_key) if order and order.status == "pending" else None

    def confirm(self, intent_id: str) -> Order | None:
        """Settle a pending order from Airwallex's own record of its intent. Safe to repeat.

        None when the intent is not one of ours: an event for someone else's
        payment changes nothing here.
        """
        with self._lock:
            key = self._by_intent.get(intent_id)
            if key is None:
                return None
            order = self.orders[key]
            if order.status != "pending":
                return order
            intent = self.payments.retrieve(intent_id)
            if intent.status in CANCELLED:
                return self._close(key, "failed", ("the payment was cancelled",))
            if intent.status != SUCCEEDED:
                return order  # the shopper is still paying
            decision, _ = self._decisions[key]
            check = check_paid(intent, decision, order_number=order.order_number,
                               approval_hash=order.approval_hash or "")
            if check.ok:
                self.orders[key] = replace(order, status="paid")
                return self.orders[key]
            # Money moved that the countersign did not allow: give it back, all of it.
            self.client.post("/pa/refunds/create", {
                "request_id": f"refund-{key}",  # a retried refund is the same refund
                "payment_intent_id": intent_id,
                "reason": "payment did not match the shopper's approval",
            })
            log.warning("refunded %s for order %s: %s", intent_id, order.order_number, "; ".join(check.reasons))
            return self._close(key, "refunded", check.reasons)

    def _close(self, key: str, status: str, reasons: tuple[str, ...]) -> Order:
        _, terms = self._decisions[key]
        for item in terms.items:
            self.set_stock(item.sku, self.catalog[item.sku].stock + item.quantity)
        self.problems[key] = reasons
        self.orders[key] = replace(self.orders[key], status=status)
        return self.orders[key]
