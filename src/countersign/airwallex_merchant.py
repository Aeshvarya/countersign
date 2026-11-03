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

A declined card (A16) is not the end of the order. Airwallex fails the attempt
and puts the intent back to REQUIRES_PAYMENT_METHOD, so the same intent can take
another card in Drop-in: still one intent, at most one charge. confirm() reads
the latest attempt from Airwallex's record and keeps its failure code in
`declines` until a later attempt replaces it. Nothing here retries a card.
"""

from __future__ import annotations

import logging
import threading
from dataclasses import replace
from typing import Any

from .airwallex import Airwallex, AirwallexError
from .decide import Decision, Outcome
from .fx import AirwallexFx, FxGateway
from .merchant import MERCHANT_NAME, FakeMerchant, Order
from .payments import SUCCEEDED, AirwallexPayments, Handoff, PaymentIntent, check_paid
from .terms import Terms

log = logging.getLogger("countersign.airwallex_merchant")

# Intent statuses after which no money will move.
CANCELLED = ("CANCELLED",)
# After a failed attempt the intent waits for another payment method (Airwallex, payment statuses).
REQUIRES_PAYMENT_METHOD = "REQUIRES_PAYMENT_METHOD"


def intent_of(event: dict[str, Any]) -> str | None:
    """The payment intent a webhook event is about, or None if it is about something else.

    A declined card sends payment_attempt.authorization_failed (3DS:
    payment_attempt.authentication_failed); the object is the attempt, which
    names its intent in payment_intent_id.
    """
    name = str(event.get("name", ""))
    obj = (event.get("data") or {}).get("object") or {}
    if name.startswith("payment_intent."):
        intent_id = obj.get("id")
    elif name.startswith("payment_attempt."):
        intent_id = obj.get("payment_intent_id")
    else:
        return None
    return intent_id if isinstance(intent_id, str) and intent_id else None


def failed_attempt(body: dict[str, Any]) -> tuple[bool, str | None]:
    """(failed, code): did the intent's latest attempt fail with the intent payable again, and Airwallex's code.

    The code is None when Airwallex gave none: we never make one up.
    """
    attempt = body.get("latest_payment_attempt")
    if body.get("status") != REQUIRES_PAYMENT_METHOD or not isinstance(attempt, dict):
        return False, None
    if attempt.get("status") != "FAILED":
        return False, None
    code = attempt.get("failure_code") or (attempt.get("failure_details") or {}).get("code")
    return True, (str(code) if code else None)


class AirwallexMerchant(FakeMerchant):
    def __init__(self, client: Airwallex, fx: FxGateway | None = None, merchant_id: str = MERCHANT_NAME,
                 currency: str = "USD") -> None:
        super().__init__(fx if fx is not None else AirwallexFx(client), merchant_id, currency)
        self.client = client
        self.payments = AirwallexPayments(client)
        self._decisions: dict[str, tuple[Decision, Terms]] = {}
        self._handoffs: dict[str, Handoff] = {}
        self._by_intent: dict[str, str] = {}
        self.problems: dict[str, tuple[str, ...]] = {}  # why a payment was refunded, by idempotency key
        # A pending order whose last attempt failed -> Airwallex's failure code (None if it gave none).
        self.declines: dict[str, str | None] = {}
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
            # The raw record, not payments.retrieve(): the latest attempt says whether a card was declined.
            body = self.client.get(f"/pa/payment_intents/{intent_id}")
            intent = PaymentIntent.from_api(body)
            if intent.status in CANCELLED:
                return self._close(key, "failed", ("the payment was cancelled",))
            if intent.status != SUCCEEDED:
                failed, code = failed_attempt(body)
                if failed:
                    self.declines[key] = code
                else:
                    self.declines.pop(key, None)  # a new attempt is under way, or none has been made
                return order  # the shopper is still paying, perhaps with another card
            self.declines.pop(key, None)
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

    def cancel_payment(self, idempotency_key: str) -> Order | None:
        """Close a pending payment the shopper gave up on, so no later card can pay it.

        Airwallex cancels the intent, then confirm() settles the order from its
        record: failed, with the stock back. If another card paid first, Airwallex
        refuses the cancel and the order is paid as usual. If Airwallex cannot be
        reached, the order stays pending and the caller can try again.
        """
        with self._lock:
            order = self.orders.get(idempotency_key)
            if order is None or order.status != "pending":
                return order
        try:
            self.client.post(f"/pa/payment_intents/{order.payment_id}/cancel", {
                "request_id": f"cancel-{idempotency_key}",  # a retried cancel is the same cancel
                "cancellation_reason": "the shopper cancelled the checkout",
            })
        except AirwallexError as err:
            log.warning("cancel of %s refused: %s", order.payment_id, err)
        try:
            return self.confirm(order.payment_id)
        except AirwallexError:
            return self.orders.get(idempotency_key)

    def _close(self, key: str, status: str, reasons: tuple[str, ...]) -> Order:
        _, terms = self._decisions[key]
        for item in terms.items:
            self.set_stock(item.sku, self.catalog[item.sku].stock + item.quantity)
        self.problems[key] = reasons
        self.declines.pop(key, None)
        self.orders[key] = replace(self.orders[key], status=status)
        return self.orders[key]
