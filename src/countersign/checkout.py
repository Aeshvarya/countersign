"""C6: the countersign step, run by the merchant just before it charges.

1. Already paid under this idempotency key? Return that order and its receipt;
   never pay twice. The same key under a different approval is refused.
2. Rebuild the live offer for the approved cart (fresh rate lock included).
3. decide() the approval against it.
4. Charge only on PROCEED, return the merchant's order number, and sign a
   receipt (C8) if the merchant has a signing key.

A card payment finishes later, in the shopper's browser (C9): the merchant's
pay() then returns a pending order. Its receipt is built now, from what was
decided now, but held unsigned; release() signs it once the payment has been
checked (AirwallexMerchant.confirm). A receipt never says "paid" before the
merchant has seen the money.

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
        self._held: dict[str, dict[str, Any]] = {}

    def get(self, idempotency_key: str) -> dict[str, Any] | None:
        return self._by_key.get(idempotency_key)

    def put(self, idempotency_key: str, receipt: dict[str, Any]) -> None:
        self._by_key[idempotency_key] = receipt

    # An unsigned receipt body waiting for its payment to be checked (C9).
    def hold(self, idempotency_key: str, body: dict[str, Any]) -> None:
        self._held[idempotency_key] = body

    def held(self, idempotency_key: str) -> dict[str, Any] | None:
        return self._held.get(idempotency_key)

    def drop(self, idempotency_key: str) -> None:
        self._held.pop(idempotency_key, None)


# What a key's existing order means to a retry, by order status.
_EXISTING = {
    "paid": (Outcome.PROCEED, "already paid under this idempotency key; returning that order"),
    "pending": (Outcome.PROCEED, "the payment under this idempotency key has started and is not finished yet"),
}


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
    if existing is not None and existing.approval_hash not in (None, approval.hash):
        # The same key under a different approval is not a retry. Returning the old
        # order would tell the agent "paid" for something the shopper never approved.
        return Result(Decision(Outcome.REFUSE, "this idempotency key was already used for a different approval"))
    if existing is not None:
        # A failed or refunded payment is final for its key: retrying under it must
        # not charge again, and must not read as paid either.
        outcome, reason = _EXISTING.get(existing.status, (
            Outcome.REFUSE, f"the payment under this idempotency key was {existing.status}; start a new checkout"))
        return Result(
            Decision(outcome, reason, charge=existing.amount, currency=existing.currency),
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

    order = merchant.pay(live, decision, idempotency_key, approval_hash=approval.hash)
    signed = None
    if signer is not None:
        body = receipts.build(approval=approval, live=live, decision=decision, order=order, agent=agent, decided_at=now)
        if order.status == "paid":
            signed = receipts.sign(body, signer)
            if store is not None:
                store.put(idempotency_key, signed)
        elif store is not None and hasattr(store, "hold"):
            store.hold(idempotency_key, body)
    return Result(decision, order, signed)


def release(order: Order, signer: receipts.MerchantSigner, store: ReceiptStore) -> dict[str, Any] | None:
    """Sign the held receipt for an order whose payment has now been checked.

    Only a paid order gets one, and only for the payment it was built for. A
    failed or refunded order's held receipt is dropped, never signed. Safe to
    call again: a webhook can arrive twice.
    """
    key = order.idempotency_key
    done = store.get(key)
    if done is not None:
        return done
    body = store.held(key) if hasattr(store, "held") else None
    if body is None:
        return None
    if order.status != "paid":
        store.drop(key)
        return None
    if (body["order_number"], body["payment_id"]) != (order.order_number, order.payment_id):
        raise ValueError("the held receipt is for a different order or payment")
    signed = receipts.sign(body, signer)
    store.put(key, signed)
    store.drop(key)
    return signed
