"""C4: rate locks between the merchant's currency and the shopper's.

Backed by Airwallex Multi-Currency Pricing: POST /pa/conversion_quotes/create
returns a conversion_rate (shopper per merchant unit) that holds until expires_at,
and the quote id is attached to the payment intent so the shopper is charged in
their own currency at exactly that rate.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal
from typing import Protocol

from .airwallex import Airwallex
from .terms import FxLock


class FxGateway(Protocol):
    def lock(self, merchant_currency: str, shopper_currency: str, now: datetime) -> FxLock: ...


def _parse_time(value: str) -> datetime:
    # Airwallex returns e.g. 2025-09-18T07:13:34Z or 2019-05-04T02:49:05+0000
    value = value.replace("Z", "+00:00")
    if len(value) > 5 and value[-5] in "+-" and value[-3] != ":":
        value = f"{value[:-2]}:{value[-2:]}"
    return datetime.fromisoformat(value).astimezone(timezone.utc)


class AirwallexFx:
    def __init__(self, client: Airwallex, validity: str = "HR_1"):
        self.client = client
        self.validity = validity

    def shopper_currencies(self) -> list[str]:
        return self.client.get("/pa/config/convertible_shopper_currencies").get("items", [])

    def lock(self, merchant_currency: str, shopper_currency: str, now: datetime) -> FxLock:
        body = self.client.post(
            "/pa/conversion_quotes/create",
            {
                "request_id": self.client.request_id(),
                "merchant_currency": merchant_currency,
                "shopper_currency": shopper_currency,
                "validity_period": self.validity,
            },
        )
        return FxLock(
            quote_id=body["id"],
            merchant_currency=body["merchant_currency"],
            shopper_currency=body["shopper_currency"],
            rate=Decimal(str(body["conversion_rate"])),
            expires_at=_parse_time(body["expires_at"]),
        )


class FakeFx:
    """Offline rate locks. Set .rate to move the market between calls."""

    def __init__(self, rate: Decimal | str, validity: timedelta = timedelta(hours=1)):
        self.rate = Decimal(rate)
        self.validity = validity
        self.issued = 0

    def lock(self, merchant_currency: str, shopper_currency: str, now: datetime) -> FxLock:
        self.issued += 1
        return FxLock(
            quote_id=f"cvq_fake_{self.issued}",
            merchant_currency=merchant_currency,
            shopper_currency=shopper_currency,
            rate=self.rate,
            expires_at=now + self.validity,
        )
