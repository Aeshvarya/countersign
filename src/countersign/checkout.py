"""C6: the countersign step, run by the merchant just before it charges.

1. Already paid under this idempotency key? Return that order and its receipt;
   never pay twice.
2. Rebuild the live offer for the approved cart (fresh rate lock included).
3. decide() the approval against it.
4. Charge only on PROCEED, return the merchant's order number, and sign a
   receipt (C8) if the merchant has a signing key.

Agent identity is checked one layer out, at the merchant's edge (tap.verify),
before a request ever reaches this function; the edge passes the verified
identity in so the receipt can record it.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any, Protocol

from . import receipt as receipts
from .decide import Change, Decision, Outcome, decide
from .merchant import MerchantGateway, Order
from .tap import AgentIdentity
from .terms import Approval


@dataclass(frozen=True)
class Result:
    decision: Decision
    order: Order | None = None
    receipt: dict[str, Any] | None = None


class ReceiptStore(Protocol):
    def get(self, idempotency_key: str) -> dict[str, Any] | None: ...
    def put(self, idempotency_key: str, receipt: dict[str, Any]) -> None: ...


class MemoryReceiptStore:
    def __init__(self) -> None:
        self._by_key: dict[str, dict[str, Any]] = {}

    def get(self, idempotency_key: str) -> dict[str, Any] | None:
        return self._by_key.get(idempotency_key)

    def put(self, idempotency_key: str, receipt: dict[str, Any]) -> None:
        self._by_key[idempotency_key] = receipt


def cart_of(approval: Approval) -> dict[str, int]:
    return {item.sku: item.quantity for item in approval.terms.items}


def countersign(
    merchant: MerchantGateway,
    approval: Approval,
    idempotency_key: str,
    now: datetime,
    *,
    agent: AgentIdentity | None = None,
    signer: receipts.MerchantSigner | None = None,
    store: ReceiptStore | None = None,
) -> Result:
    existing = merchant.find_order(idempotency_key)
    if existing is not None:
        return Result(
            Decision(Outcome.PROCEED, "already paid under this idempotency key; returning that order",
                     charge=existing.amount, currency=existing.currency),
            existing,
            store.get(idempotency_key) if store else None,
        )

    try:
        live = merchant.offer(cart_of(approval), approval.terms.shopper_currency, now)
    except LookupError as err:
        return Result(Decision(Outcome.REASK, "an item is no longer available", (Change("stock", "available", str(err)),)))

    decision = decide(approval, live, now)
    if decision.outcome is not Outcome.PROCEED:
        return Result(decision)

    order = merchant.pay(live, decision, idempotency_key)
    signed = None
    if signer is not None:
        body = receipts.build(approval=approval, live=live, decision=decision, order=order, agent=agent, decided_at=now)
        signed = receipts.sign(body, signer)
        if store is not None:
            store.put(idempotency_key, signed)
    return Result(decision, order, signed)
