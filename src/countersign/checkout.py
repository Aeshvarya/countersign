"""C6: the countersign step, run by the merchant just before it charges.

1. Already paid under this idempotency key? Return that order; never pay twice.
2. Rebuild the live offer for the approved cart (fresh rate lock included).
3. decide() the approval against it.
4. Charge only on PROCEED, and return the merchant's order number.

Agent identity is checked one layer out, at the merchant's edge (tap.verify),
before a request ever reaches this function.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from .decide import Change, Decision, Outcome, decide
from .merchant import MerchantGateway, Order
from .terms import Approval


@dataclass(frozen=True)
class Result:
    decision: Decision
    order: Order | None = None


def cart_of(approval: Approval) -> dict[str, int]:
    return {item.sku: item.quantity for item in approval.terms.items}


def countersign(merchant: MerchantGateway, approval: Approval, idempotency_key: str, now: datetime) -> Result:
    existing = merchant.find_order(idempotency_key)
    if existing is not None:
        return Result(
            Decision(Outcome.PROCEED, "already paid under this idempotency key; returning that order",
                     charge=existing.amount, currency=existing.currency),
            existing,
        )

    try:
        live = merchant.offer(cart_of(approval), approval.terms.shopper_currency, now)
    except LookupError as err:
        return Result(Decision(Outcome.REASK, "an item is no longer available", (Change("stock", "available", str(err)),)))

    decision = decide(approval, live, now)
    if decision.outcome is not Outcome.PROCEED:
        return Result(decision)
    return Result(decision, merchant.pay(live, decision, idempotency_key))
