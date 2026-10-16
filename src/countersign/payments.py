"""A6: taking the card payment through Airwallex, and checking it afterwards.

The card is never seen by the agent or by us. The flow (Airwallex Payments with
Multi-Currency Pricing and the Drop-in element):

1. After the countersign says PROCEED, the merchant creates a payment intent in
   its own currency, carrying the order number and the approval hash.
2. The shopper's browser mounts Airwallex's Drop-in element with the intent's
   client secret and the locked conversion quote (`quote_id`). Drop-in applies
   the quote itself, so the shopper is charged in their own currency at exactly
   the locked rate.
3. The merchant fetches the intent again and checks it, field by field, against
   what the countersign decided (`check_paid`). Only then is the order paid and
   the receipt signed. A browser "success" event is never trusted on its own.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import Any

from .airwallex import Airwallex
from .decide import Decision, Outcome
from .terms import minor_units

SUCCEEDED = "SUCCEEDED"
TEST_CARD = "4035501000000008"  # Airwallex sandbox test card, from the builder guide


@dataclass(frozen=True)
class PaymentIntent:
    id: str
    status: str
    amount: Decimal
    currency: str
    merchant_order_id: str | None
    client_secret: str | None = None
    conversion_quote_id: str | None = None
    metadata: dict[str, Any] | None = None

    @classmethod
    def from_api(cls, body: dict[str, Any]) -> "PaymentIntent":
        return cls(
            id=body["id"],
            status=body["status"],
            amount=Decimal(str(body["amount"])),
            currency=body["currency"],
            merchant_order_id=body.get("merchant_order_id"),
            client_secret=body.get("client_secret"),
            conversion_quote_id=body.get("conversion_quote_id"),
            metadata=body.get("metadata"),
        )


@dataclass(frozen=True)
class Handoff:
    """What the shopper's browser needs to mount Drop-in. Nothing secret to the merchant."""

    intent_id: str
    client_secret: str
    currency: str  # the merchant currency the intent was created in
    quote_id: str | None  # the locked conversion quote; Drop-in applies it

    def as_json(self) -> dict[str, Any]:
        return {"intent_id": self.intent_id, "client_secret": self.client_secret,
                "currency": self.currency, "quote_id": self.quote_id}


class AirwallexPayments:
    def __init__(self, client: Airwallex):
        self.client = client

    def create(self, *, amount: Decimal, currency: str, merchant_order_id: str, request_id: str,
               metadata: dict[str, str] | None = None, return_url: str | None = None) -> PaymentIntent:
        body: dict[str, Any] = {
            "request_id": request_id,
            "amount": float(amount),  # major units, as the API expects
            "currency": currency,
            "merchant_order_id": merchant_order_id,
            "metadata": metadata or {},
        }
        if return_url:
            body["return_url"] = return_url
        return PaymentIntent.from_api(self.client.post("/pa/payment_intents/create", body))

    def retrieve(self, intent_id: str) -> PaymentIntent:
        return PaymentIntent.from_api(self.client.get(f"/pa/payment_intents/{intent_id}"))

    def attach_quote(self, intent_id: str, quote_id: str, request_id: str) -> PaymentIntent:
        """For the Card element. Drop-in applies the quote itself; don't call this with Drop-in."""
        return PaymentIntent.from_api(self.client.post(
            f"/pa/payment_intents/{intent_id}/update", {"request_id": request_id, "conversion_quote_id": quote_id}))

    def start(self, decision: Decision, *, merchant_total: Decimal, merchant_currency: str, order_number: str,
              approval_hash: str, idempotency_key: str) -> Handoff:
        """Create the intent for a PROCEED decision and return the Drop-in handoff.

        The idempotency key doubles as the Airwallex request_id, so a retried start
        returns the same intent instead of creating a second one.
        """
        if decision.outcome is not Outcome.PROCEED:
            raise PermissionError(f"no payment without a proceed decision: {decision.reason}")
        intent = self.create(amount=merchant_total, currency=merchant_currency, merchant_order_id=order_number,
                             request_id=idempotency_key,
                             metadata={"approval_hash": approval_hash, "idempotency_key": idempotency_key})
        if not intent.client_secret:
            raise RuntimeError("Airwallex returned no client secret for the payment intent")
        return Handoff(intent.id, intent.client_secret, merchant_currency, decision.fx_quote_id)


@dataclass(frozen=True)
class PaidCheck:
    ok: bool
    reasons: tuple[str, ...]


def check_paid(intent: PaymentIntent, decision: Decision, *, order_number: str, approval_hash: str) -> PaidCheck:
    """Did Airwallex take exactly the payment the countersign allowed? Every field, or no."""
    problems: list[str] = []
    if intent.status != SUCCEEDED:
        problems.append(f"the payment is {intent.status}, not {SUCCEEDED}")
    if intent.merchant_order_id != order_number:
        problems.append(f"the payment is for order {intent.merchant_order_id}, not {order_number}")
    if (intent.metadata or {}).get("approval_hash") != approval_hash:
        problems.append("the payment does not carry this approval's hash")
    if decision.currency is None or intent.currency != decision.currency:
        problems.append(f"charged in {intent.currency}, approved in {decision.currency}")
    elif decision.charge is not None:
        tolerance = Decimal(1).scaleb(-minor_units(decision.currency))
        if abs(intent.amount - decision.charge) > tolerance:
            problems.append(f"charged {intent.amount} {intent.currency}, the countersign allowed {decision.charge}")
    if decision.fx_quote_id and intent.conversion_quote_id not in (None, decision.fx_quote_id):
        problems.append("the payment used a different rate lock than the one approved")
    return PaidCheck(not problems, tuple(problems) or ("Airwallex took exactly the payment the countersign allowed",))
