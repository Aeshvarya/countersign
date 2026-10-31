"""C1: the terms a shopper approves, and the hash that binds the approval to them.

Everything that moves money is checked against an Approval. An Approval holds the
exact Terms the shopper saw, a hash of the fields they decided on, and a ceiling:
the most they agreed to pay, in their own currency.

Amounts are Decimals in major units (100 means 100.00), matching the Airwallex API.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from datetime import datetime, timezone
from decimal import ROUND_HALF_EVEN, Decimal

# Minor units for the currencies that differ from the default of 2. Airwallex's
# rounding page, not ISO 4217, is the authority: it treats HUF, TWD and ISK as
# zero-decimal, so a shopper charged in them is charged in whole units.
_MINOR_UNITS = {"JPY": 0, "KRW": 0, "VND": 0, "IDR": 0, "CLP": 0, "HUF": 0, "TWD": 0, "ISK": 0,
                "BHD": 3, "KWD": 3, "OMR": 3}


def minor_units(currency: str) -> int:
    return _MINOR_UNITS.get(currency.upper(), 2)


def money(amount: Decimal | str | int, currency: str) -> Decimal:
    """Round an amount to its currency's minor unit, half to even, as Airwallex does.

    With half up, an exact tie showed the shopper one minor unit more than
    Airwallex then charged (C9, agreed 2026-10-29).
    """
    exponent = Decimal(1).scaleb(-minor_units(currency))
    return Decimal(amount).quantize(exponent, rounding=ROUND_HALF_EVEN)


def utc(dt: datetime) -> datetime:
    if dt.tzinfo is None:
        raise ValueError("datetimes must be timezone-aware")
    return dt.astimezone(timezone.utc)


@dataclass(frozen=True)
class LineItem:
    sku: str
    title: str
    quantity: int
    unit_price: Decimal  # merchant currency


@dataclass(frozen=True)
class FxLock:
    """A locked conversion quote: shopper_amount = merchant_amount * rate."""

    quote_id: str
    merchant_currency: str
    shopper_currency: str
    rate: Decimal
    expires_at: datetime

    def is_valid(self, now: datetime) -> bool:
        return utc(now) < utc(self.expires_at)


@dataclass(frozen=True)
class Terms:
    """What the shopper is shown, or what the merchant is offering right now."""

    merchant_id: str
    items: tuple[LineItem, ...]
    delivery: str
    merchant_currency: str
    merchant_total: Decimal
    shopper_currency: str
    shopper_total: Decimal
    fx: FxLock | None = None

    def computed_merchant_total(self) -> Decimal:
        total = sum((i.unit_price * i.quantity for i in self.items), Decimal(0))
        return money(total, self.merchant_currency)

    def computed_shopper_total(self) -> Decimal | None:
        """The shopper total implied by the merchant total and the locked rate.

        None when the currencies differ and there is no rate lock: there is no
        honest way to state a price in the shopper's currency without one.
        """
        if self.shopper_currency == self.merchant_currency:
            return money(self.merchant_total, self.shopper_currency)
        if self.fx is None:
            return None
        return money(self.merchant_total * self.fx.rate, self.shopper_currency)


# The fields a shopper actually decides on. The rate lock is how we deliver the
# price, not part of the decision: a fresh quote that stays under the ceiling is
# still what they agreed to.
BOUND_FIELDS = ("merchant_id", "items", "delivery", "merchant_currency", "merchant_total", "shopper_currency")


def _plain(value):
    if isinstance(value, Decimal):
        return format(value.normalize(), "f")
    if isinstance(value, LineItem):
        return {
            "sku": value.sku,
            "title": value.title,
            "quantity": value.quantity,
            "unit_price": _plain(value.unit_price),
        }
    if isinstance(value, tuple):
        return [_plain(v) for v in value]
    if isinstance(value, datetime):
        return utc(value).isoformat()
    return value


def canonical(terms: Terms, ceiling: Decimal) -> str:
    """Deterministic JSON of the bound fields plus the ceiling."""
    body = {name: _plain(getattr(terms, name)) for name in BOUND_FIELDS}
    body["ceiling"] = _plain(money(ceiling, terms.shopper_currency))
    return json.dumps(body, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def terms_hash(terms: Terms, ceiling: Decimal) -> str:
    return hashlib.sha256(canonical(terms, ceiling).encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class Approval:
    terms: Terms
    ceiling: Decimal  # shopper currency
    approved_at: datetime
    expires_at: datetime
    approver: str
    hash: str = field(default="")

    @classmethod
    def grant(cls, terms: Terms, ceiling: Decimal, approver: str, approved_at: datetime, expires_at: datetime) -> "Approval":
        ceiling = money(ceiling, terms.shopper_currency)
        if ceiling < terms.shopper_total:
            raise ValueError("the ceiling is below the price the shopper was shown")
        return cls(terms, ceiling, approved_at, expires_at, approver, terms_hash(terms, ceiling))

    def is_intact(self) -> bool:
        return self.hash == terms_hash(self.terms, self.ceiling)
