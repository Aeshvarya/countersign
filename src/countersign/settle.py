"""W3 (C9 extended): settle a payment Airwallex's MCP made, against the quote our create_checkout gave it.

On Kit 10's path Airwallex's MCP creates and charges the payment intent itself, so
we never start it and never see the card. What ties the intent back to our quote,
read from the first live one (2026-11-05, int_sgpvhhzcphnvd9yy4qd, through
create_checkout_session, a delegate token and complete_checkout_session):

- the intent's merchant_order_id is our checkout id (chk_...), the id our
  create_checkout answered, and complete_checkout_session gives the agent that
  same id as the order's id;
- its amount and currency are the quote's items total plus the chosen
  fulfillment option's cost, in the currency quoted;
- latest_payment_attempt.agentic_payment_data.fulfillment[].selected_option_id
  is the option in our own words (standard, express). The ids the MCP shows the
  agent encode ours (base64url of option.method.destination); they never reach us.

settle() treats the webhook as a nudge: it fetches the intent, finds the quote and
checks every field. All match: paid, and the merchant signs a receipt for it. Any
mismatch after money moved: refunded in full, no receipt. That includes an intent
paid after its quote expired, and a second payment for a checkout already paid.
A quote we no longer hold is different (Varunika's review, 11-05): an intent made
before this process started may hold the receipt the process before a restart
signed, so it is logged and shown as unverified, never refunded blind. One made
since, with no quote, is not a checkout we priced, so it is refunded.

The receipt is C8's, with one addition. Its approval and `charged` are the quoted
items, the terms the countersign priced; `fulfillment` is the shipping the shopper
chose on Airwallex's side, so charged.amount + fulfillment.amount is what the
card paid.
"""

from __future__ import annotations

import logging
import threading
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from typing import Any, Callable

from . import receipt as receipts
from .airwallex import Airwallex
from .checkout import ReceiptStore
from .decide import Decision, Outcome
from .edge.acp import QuoteRecord, from_minor, to_minor
from .merchant import Order
from .payments import SUCCEEDED

log = logging.getLogger("countersign.settle")

CHECKOUT_PREFIX = "chk_"  # every create_checkout id (edge/acp.py)


@dataclass(frozen=True)
class Settlement:
    checkout_id: str
    intent_id: str
    status: str  # "paid", "refunded", "unverified" (quote lost in a restart) or "pending" (not succeeded yet)
    amount: Decimal | None
    currency: str | None
    reasons: tuple[str, ...] = ()  # why it was refunded
    receipt: dict[str, Any] | None = None


def selected_option(intent: dict[str, Any]) -> str | None:
    """The fulfillment option the shopper chose, from the intent's latest attempt, or None."""
    attempt = intent.get("latest_payment_attempt") or {}
    groups = (attempt.get("agentic_payment_data") or {}).get("fulfillment") or []
    chosen = {g.get("selected_option_id") for g in groups if isinstance(g, dict)} - {None}
    return chosen.pop() if len(chosen) == 1 else None


def _when(value: Any) -> datetime | None:
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None


def problems(intent: dict[str, Any], quote: QuoteRecord) -> tuple[str, ...]:
    """Why this succeeded intent is not the payment the quote allowed. Empty when it is."""
    if quote.terms is None or quote.approval is None:
        return ("nothing in the cart was available when it was quoted",)
    reasons = []
    currency = intent.get("currency")
    if currency != quote.currency:
        reasons.append(f"paid in {currency}, quoted in {quote.currency}")
    option = selected_option(intent)
    if quote.options and option not in quote.options:
        reasons.append(f"fulfillment option {option!r} was never quoted" if option else "no fulfillment option was chosen")
    elif currency == quote.currency:
        expected = quote.total_minor + quote.options.get(option, 0)
        try:
            paid = to_minor(Decimal(str(intent.get("amount"))), quote.currency)
        except (InvalidOperation, ValueError):
            reasons.append(f"the amount {intent.get('amount')!r} is not a whole number of minor units")
        else:
            if paid != expected:
                reasons.append(f"paid {from_minor(paid, quote.currency)} {quote.currency} for a quote of "
                               f"{from_minor(expected, quote.currency)} {quote.currency}")
    created = _when(intent.get("created_at"))
    if created is None:
        reasons.append("the intent has no readable created_at")
    elif created > quote.expires_at:
        reasons.append(f"paid after the quote expired at {quote.expires_at.isoformat()}")
    return tuple(reasons)


def receipt_body(intent: dict[str, Any], quote: QuoteRecord) -> dict[str, Any]:
    """The receipt for a payment that passed problems(): C8's, plus the shipping chosen."""
    terms, approval = quote.terms, quote.approval
    option = selected_option(intent)
    order = Order(order_number=quote.checkout_id, payment_id=str(intent["id"]), status="paid",
                  amount=Decimal(str(intent["amount"])), currency=quote.currency,
                  idempotency_key=quote.checkout_id, approval_hash=approval.hash)
    decision = Decision(Outcome.PROCEED, "paid as quoted", charge=terms.shopper_total, currency=quote.currency,
                        fx_quote_id=terms.fx.quote_id if terms.fx else None)
    body = receipts.build(approval=approval, live=terms, decision=decision, order=order, agent=None,
                          decided_at=quote.created)
    body["fulfillment"] = {
        "option_id": option,
        "amount": format(from_minor(quote.options.get(option, 0), quote.currency), "f"),
        "currency": quote.currency,
    }
    return body


class Settler:
    """Settles the intents Airwallex's MCP creates for our create_checkout quotes. Safe to repeat."""

    def __init__(self, client: Airwallex, quote_for: Callable[[str], QuoteRecord | None],
                 signer: receipts.MerchantSigner, store: ReceiptStore,
                 clock: Callable[[], datetime] = lambda: datetime.now(timezone.utc)) -> None:
        self.client = client
        self.quote_for = quote_for
        self.signer = signer
        self.store = store
        self.started = clock()  # quotes made before this are gone after a restart
        self.settled: dict[str, Settlement] = {}  # by intent id, once paid, refunded or unverified
        self.paid: dict[str, str] = {}  # checkout id -> the one intent that paid it
        self._lock = threading.Lock()

    def settle(self, intent_id: str) -> Settlement | None:
        """Paid, refunded or pending, from Airwallex's own record. None if the intent is not an ACP checkout of ours."""
        with self._lock:
            done = self.settled.get(intent_id)
            if done is not None:
                return done
            intent = self.client.get(f"/pa/payment_intents/{intent_id}")
            checkout_id = intent.get("merchant_order_id")
            if not isinstance(checkout_id, str) or not checkout_id.startswith(CHECKOUT_PREFIX):
                return None
            amount = Decimal(str(intent["amount"])) if intent.get("amount") is not None else None
            currency = intent.get("currency")
            if intent.get("status") != SUCCEEDED:
                return Settlement(checkout_id, intent_id, "pending", amount, currency)
            quote = self.quote_for(checkout_id)
            created = _when(intent.get("created_at"))
            if quote is None and (created is None or created < self.started):
                # Priced by the process before a restart, which may have receipted it: never refund it blind.
                log.warning("cannot verify %s for %s: its quote is from before this process started", intent_id, checkout_id)
                done = Settlement(checkout_id, intent_id, "unverified", amount, currency,
                                  ("its quote was made before the service restarted, so it can't be checked; "
                                   "nothing was refunded",))
                self.settled[intent_id] = done
                return done
            first = self.paid.get(checkout_id)
            if first is not None and first != intent_id:
                reasons: tuple[str, ...] = (f"this checkout was already paid by {first}",)
            elif quote is None:
                reasons = ("no quote on record for this checkout",)
            else:
                reasons = problems(intent, quote)
            if reasons:
                # Money moved that no countersign allowed: give it back, all of it.
                self.client.post("/pa/refunds/create", {
                    "request_id": f"refund-{intent_id}",  # a retried refund is the same refund
                    "payment_intent_id": intent_id,
                    "reason": "payment did not match the merchant's quote",
                })
                log.warning("refunded %s for %s: %s", intent_id, checkout_id, "; ".join(reasons))
                done = Settlement(checkout_id, intent_id, "refunded", amount, currency, reasons)
            else:
                signed = receipts.sign(receipt_body(intent, quote), self.signer)
                self.store.put(checkout_id, signed)
                self.paid[checkout_id] = intent_id
                done = Settlement(checkout_id, intent_id, "paid", amount, currency, receipt=signed)
            self.settled[intent_id] = done
            return done
