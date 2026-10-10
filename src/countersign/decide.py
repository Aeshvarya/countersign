"""C5: the one function that decides whether money moves.

decide() compares what the shopper approved with what the merchant is offering
now, and returns exactly one of three outcomes:

  PROCEED  charge `charge` in the shopper's currency under `fx_quote_id`
  REASK    something the shopper decided on changed; show them `changes`
  REFUSE   the request itself is broken (tampered approval, inconsistent
           numbers, stale rate lock); nothing is shown to the shopper as a choice

No model output reaches this function. Every input is data the code computed or
the merchant API returned.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from enum import Enum

from .terms import BOUND_FIELDS, Approval, Terms, minor_units, utc


class Outcome(str, Enum):
    PROCEED = "proceed"
    REASK = "reask"
    REFUSE = "refuse"


@dataclass(frozen=True)
class Change:
    field: str
    approved: object
    live: object


@dataclass(frozen=True)
class Decision:
    outcome: Outcome
    reason: str
    changes: tuple[Change, ...] = ()
    charge: Decimal | None = None
    currency: str | None = None
    fx_quote_id: str | None = None


def _refuse(reason: str) -> Decision:
    return Decision(Outcome.REFUSE, reason)


def diff(approved: Terms, live: Terms) -> tuple[Change, ...]:
    return tuple(
        Change(name, getattr(approved, name), getattr(live, name))
        for name in BOUND_FIELDS
        if getattr(approved, name) != getattr(live, name)
    )


def decide(approval: Approval, live: Terms, now: datetime) -> Decision:
    now = utc(now)

    if not approval.is_intact():
        return _refuse("the approval does not match its own hash")

    # The live offer must be internally consistent before it is compared.
    if live.computed_merchant_total() != live.merchant_total:
        return _refuse("the merchant total does not equal the sum of its lines")
    if live.fx is not None:
        if (live.fx.merchant_currency, live.fx.shopper_currency) != (live.merchant_currency, live.shopper_currency):
            return _refuse("the rate lock is for a different currency pair")
        if not live.fx.is_valid(now):
            return _refuse("the rate lock has expired; fetch a new quote before deciding")
    expected = live.computed_shopper_total()
    if expected is None:
        return _refuse("the currencies differ and there is no rate lock")
    one_minor_unit = Decimal(1).scaleb(-minor_units(live.shopper_currency))
    if abs(expected - live.shopper_total) > one_minor_unit:
        return _refuse("the shopper total does not follow from the locked rate")

    if now >= utc(approval.expires_at):
        return Decision(Outcome.REASK, "the approval has expired")

    changes = diff(approval.terms, live)
    if changes:
        return Decision(Outcome.REASK, "the terms changed after approval", changes)

    if live.shopper_total > approval.ceiling:
        return Decision(
            Outcome.REASK,
            "the price in the shopper's currency is above what they approved",
            (Change("shopper_total", approval.ceiling, live.shopper_total),),
        )

    return Decision(
        Outcome.PROCEED,
        "the terms match the approval and the price is within the ceiling",
        charge=live.shopper_total,
        currency=live.shopper_currency,
        fx_quote_id=live.fx.quote_id if live.fx else None,
    )
