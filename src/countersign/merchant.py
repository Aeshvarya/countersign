"""C3: the merchant side, as one interface with an offline fake.

The agent only ever talks to a MerchantGateway. Today that is FakeMerchant (in
memory, deterministic, no keys). When Airwallex enables merchant-side Agentic
Commerce, AirwallexMerchant implements the same methods against the merchant
MCP and the payment intent API, and nothing above this layer changes.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, replace
from datetime import datetime
from decimal import Decimal
from typing import Protocol

from .decide import Decision, Outcome
from .fx import FxGateway
from .terms import LineItem, Terms, money


# The synthetic shop's name, shown to the shopper and the agent and bound into the terms. A real-sounding
# brand: with an id like "merchant_demo" the agent told shoppers it was a test store.
MERCHANT_NAME = "Larch Lane"


@dataclass(frozen=True)
class Product:
    sku: str
    title: str
    category: str
    price: Decimal  # merchant currency
    stock: int
    delivery: str


@dataclass(frozen=True)
class Order:
    order_number: str  # the merchant's order number, not a payment intent id
    payment_id: str
    status: str  # "paid" or "failed"
    amount: Decimal
    currency: str
    idempotency_key: str
    approval_hash: str | None = None  # the approval this order was charged under


class MerchantGateway(Protocol):
    merchant_id: str
    currency: str

    def search(self, query: str, limit: int = 5) -> list[Product]: ...

    def offer(self, cart: dict[str, int], shopper_currency: str, now: datetime) -> Terms:
        """Live terms for this cart right now, with a fresh rate lock if needed."""
        ...

    def find_order(self, idempotency_key: str) -> Order | None:
        """Check before retrying: a timeout is not proof the payment failed."""
        ...

    def pay(self, terms: Terms, decision: Decision, idempotency_key: str, approval_hash: str | None = None) -> Order: ...


# ------------------------------------------------------------------ offline fake

_CATEGORIES = {
    "audio": ["Wireless earbuds", "Over-ear headphones", "Bluetooth speaker", "USB microphone", "DAC amplifier"],
    "desk": ["Mechanical keyboard", "Laptop stand", "Desk lamp", "Monitor arm", "Cable tray"],
    "travel": ["Carry-on backpack", "Packing cubes", "Universal adapter", "Neck pillow", "Toiletry bag"],
    "kitchen": ["Pour-over kettle", "Burr grinder", "French press", "Chef's knife", "Cast iron pan"],
    "fitness": ["Resistance bands", "Jump rope", "Yoga mat", "Adjustable dumbbell", "Foam roller"],
}
_VARIANTS = ["Lite", "Standard", "Pro", "Max"]
_DELIVERY = ["standard, 5-7 business days", "express, 2 business days"]


def synthetic_catalog() -> list[Product]:
    """100 deterministic synthetic products (the merchant-side kit asks for at least 100)."""
    products = []
    for category, names in _CATEGORIES.items():
        for name in names:
            for v, variant in enumerate(_VARIANTS):
                sku = f"{category[:3].upper()}-{abs(hash_int(name)) % 1000:03d}-{v}"
                base = 12 + hash_int(name) % 140
                products.append(
                    Product(
                        sku=sku,
                        title=f"{name} {variant}",
                        category=category,
                        price=money(Decimal(base) * Decimal(1 + 0.35 * v) - Decimal("0.01"), "USD"),
                        stock=5 + hash_int(sku) % 40,
                        delivery=_DELIVERY[hash_int(sku) % 2],
                    )
                )
    return products


def hash_int(text: str) -> int:
    return int(hashlib.sha256(text.encode()).hexdigest()[:8], 16)


class FakeMerchant:
    def __init__(self, fx: FxGateway, merchant_id: str = MERCHANT_NAME, currency: str = "USD"):
        self.merchant_id = merchant_id
        self.currency = currency
        self.fx = fx
        self.catalog = {p.sku: p for p in synthetic_catalog()}
        self.orders: dict[str, Order] = {}

    # Demo levers: change the world between approval and payment.
    def set_price(self, sku: str, price: Decimal | str) -> None:
        self.catalog[sku] = replace(self.catalog[sku], price=money(price, self.currency))

    def set_stock(self, sku: str, stock: int) -> None:
        self.catalog[sku] = replace(self.catalog[sku], stock=stock)

    def set_delivery(self, sku: str, delivery: str) -> None:
        self.catalog[sku] = replace(self.catalog[sku], delivery=delivery)

    def search(self, query: str, limit: int = 5) -> list[Product]:
        words = query.lower().split()
        scored = [
            (sum(w in f"{p.title} {p.category}".lower() for w in words), p)
            for p in self.catalog.values()
            if p.stock > 0
        ]
        return [p for score, p in sorted(scored, key=lambda s: (-s[0], s[1].price)) if score][:limit]

    def offer(self, cart: dict[str, int], shopper_currency: str, now: datetime) -> Terms:
        items, deliveries = [], set()
        for sku, qty in sorted(cart.items()):
            product = self.catalog[sku]
            if product.stock < qty:
                raise LookupError(f"{sku} has {product.stock} in stock, {qty} requested")
            items.append(LineItem(sku, product.title, qty, product.price))
            deliveries.add(product.delivery)
        merchant_total = money(sum((i.unit_price * i.quantity for i in items), Decimal(0)), self.currency)
        fx = None if shopper_currency == self.currency else self.fx.lock(self.currency, shopper_currency, now)
        terms = Terms(
            merchant_id=self.merchant_id,
            items=tuple(items),
            delivery=" + ".join(sorted(deliveries)),
            merchant_currency=self.currency,
            merchant_total=merchant_total,
            shopper_currency=shopper_currency,
            shopper_total=merchant_total,
            fx=fx,
        )
        return replace(terms, shopper_total=terms.computed_shopper_total())

    def find_order(self, idempotency_key: str) -> Order | None:
        return self.orders.get(idempotency_key)

    def pay(self, terms: Terms, decision: Decision, idempotency_key: str, approval_hash: str | None = None) -> Order:
        if decision.outcome is not Outcome.PROCEED:
            raise PermissionError(f"refusing to charge: {decision.reason}")
        existing = self.find_order(idempotency_key)
        if existing:
            return existing
        for item in terms.items:
            self.set_stock(item.sku, self.catalog[item.sku].stock - item.quantity)
        order = Order(
            order_number=f"ORD-{len(self.orders) + 1001}",
            payment_id=f"int_fake_{hash_int(idempotency_key):08x}",
            status="paid",
            amount=decision.charge,
            currency=decision.currency,
            idempotency_key=idempotency_key,
            approval_hash=approval_hash,
        )
        self.orders[idempotency_key] = order
        return order
