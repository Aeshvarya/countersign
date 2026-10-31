"""V6: change the merchant's price, stock or delivery on cue, for the demo and the video.

The merchant behind /acp (the house) keeps its catalogue in memory inside this
service, so a change has to happen here. One set of functions does it, and two
doors call them:

    POST /api/levers/{price,stock,delivery}   for a script, behind COUNTERSIGN_LEVER_TOKEN
    the merchant console (/console)           the same functions, the same token

A change lands on the live catalogue at once: the next offer, quote or complete
prices from it, so a shopper who approved the old terms is asked again and
nothing is charged (the countersign). With COUNTERSIGN_SYNC_CATALOG=1 and
sandbox keys, the product is also upserted to Airwallex's catalogue, all of its
variants in one call (C11: an upsert replaces a product with all its variants),
so the merchant MCP's search shows the new value too.

    POST /api/levers/reset   puts products back as uploaded, so the demo can run again
    scripts/change_event.py  the same routes from a terminal, when the console is not open
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from decimal import Decimal, InvalidOperation
from typing import Any

from .. import catalog_gmc
from ..merchant import synthetic_catalog
from ..terms import minor_units, money

MAX_STOCK = 1_000_000
MAX_DELIVERY = 80


class LeverError(ValueError):
    """A lever call that cannot be applied: unknown product or a bad value. Nothing changed."""


@dataclass
class Change:
    sku: str
    field: str
    before: str
    after: str
    title: str
    sync: dict[str, Any] | None = field(default=None)

    def as_json(self) -> dict[str, Any]:
        return asdict(self)


def _product(house: Any, sku: Any):
    catalog = getattr(house.merchant, "catalog", None)
    if not isinstance(sku, str) or not isinstance(catalog, dict) or sku not in catalog:
        raise LeverError(f"no product with id {sku!r}")
    return catalog[sku]


def set_price(house: Any, sku: str, price: Any) -> Change:
    product = _product(house, sku)
    currency = house.merchant.currency
    try:
        value = Decimal(str(price))
    except InvalidOperation:
        raise LeverError("price must be a number") from None
    if not value.is_finite() or value <= 0 or value > Decimal("1000000"):
        raise LeverError("price must be above 0 and at most 1000000")
    if value != money(value, currency):
        raise LeverError(f"price has more decimals than {currency} allows ({minor_units(currency)})")
    house.merchant.set_price(sku, value)
    return _done(house, sku, "price", f"{product.price} {currency}", f"{money(value, currency)} {currency}")


def raise_price(house: Any, sku: str, percent: Any) -> Change:
    """The demo's "price went up": percent of the current price, rounded to the currency."""
    try:
        pct = Decimal(str(percent))
    except InvalidOperation:
        raise LeverError("percent must be a number") from None
    if not pct.is_finite() or not Decimal("-90") <= pct <= Decimal("500"):
        raise LeverError("percent must be between -90 and 500")
    product = _product(house, sku)
    return set_price(house, sku, money(product.price * (1 + pct / 100), house.merchant.currency))


def set_stock(house: Any, sku: str, stock: Any) -> Change:
    product = _product(house, sku)
    if isinstance(stock, bool) or not isinstance(stock, int) or not 0 <= stock <= MAX_STOCK:
        raise LeverError(f"stock must be a whole number from 0 to {MAX_STOCK}")
    house.merchant.set_stock(sku, stock)
    return _done(house, sku, "stock", str(product.stock), str(stock))


def set_delivery(house: Any, sku: str, delivery: Any) -> Change:
    product = _product(house, sku)
    if not isinstance(delivery, str) or not delivery.strip() or len(delivery) > MAX_DELIVERY:
        raise LeverError(f"delivery must be text of 1 to {MAX_DELIVERY} characters")
    house.merchant.set_delivery(sku, delivery.strip())
    return _done(house, sku, "delivery", product.delivery, delivery.strip())


def reset(house: Any, sku: Any = None) -> list[Change]:
    """Put products back as they were uploaded (`synthetic_catalog()`), so the demo can run again.

    One product, or with no sku every product a lever or a purchase moved. Each product's
    variant group is upserted once, after all its fields are back. Reset between takes, not
    during one: a card payment still pending holds stock, and if it then fails the hold comes
    back on top of the reset value.
    """
    original = {p.sku: p for p in synthetic_catalog()}
    if sku is None:
        skus = [s for s in house.merchant.catalog if s in original]
    else:
        _product(house, sku)
        if sku not in original:
            raise LeverError(f"{sku!r} is not in the uploaded catalogue")
        skus = [sku]
    merchant, currency = house.merchant, house.merchant.currency
    moved: dict[str, list[Change]] = {}
    for s in skus:
        now, was = merchant.catalog[s], original[s]
        changes = []
        if now.price != was.price:
            merchant.set_price(s, was.price)
            changes.append(Change(s, "price", f"{now.price} {currency}", f"{was.price} {currency}", now.title))
        if now.stock != was.stock:
            merchant.set_stock(s, was.stock)
            changes.append(Change(s, "stock", str(now.stock), str(was.stock), now.title))
        if now.delivery != was.delivery:
            merchant.set_delivery(s, was.delivery)
            changes.append(Change(s, "delivery", now.delivery, was.delivery, now.title))
        if changes:
            moved[s] = changes
    synced: dict[str, dict[str, Any]] = {}
    feed = getattr(house, "feed", None)
    for s, changes in moved.items():
        sync = None
        if getattr(house, "sync_catalog", False):
            group = catalog_gmc.group_id(s)
            if group not in synced:
                synced[group] = sync_product(house, s)
            sync = synced[group]
        for change in changes:
            change.sync = sync
        if feed is not None:
            feed.add("lever", "info", f"Merchant reset {changes[0].title}",
                     " · ".join(f"{c.field} {c.before} → {c.after}" for c in changes) + _sync_note(sync), sku=s)
    return [c for changes in moved.values() for c in changes]


def _done(house: Any, sku: str, name: str, before: str, after: str) -> Change:
    change = Change(sku, name, before, after, house.merchant.catalog[sku].title)
    if getattr(house, "sync_catalog", False):
        change.sync = sync_product(house, sku)
    feed = getattr(house, "feed", None)
    if feed is not None:
        feed.add("lever", "info", f"Merchant changed {name} of {change.title}",
                 f"{before} → {after}" + _sync_note(change.sync), sku=sku)
    return change


def _sync_note(sync: dict[str, Any] | None) -> str:
    if sync is None:
        return ""
    if sync.get("ok"):
        return f" · Airwallex catalogue updated ({sync['rows']} rows)"
    return f" · Airwallex catalogue NOT updated: {sync.get('error', 'failed rows')}"


def sync_product(house: Any, sku: str) -> dict[str, Any]:
    """Upsert this product's whole variant group to Airwallex. Never raises: the result says what happened."""
    client = getattr(house, "airwallex", None)
    base = getattr(house, "public_url", None)
    if client is None or not base:
        return {"ok": False, "rows": 0, "error": "needs sandbox keys and COUNTERSIGN_PUBLIC_URL"}
    group = catalog_gmc.group_id(sku)
    rows = [catalog_gmc.to_gmc(p, base_url=base, currency=house.merchant.currency)
            for p in house.merchant.catalog.values() if catalog_gmc.group_id(p.sku) == group]
    try:
        result = catalog_gmc.upload(rows, client.post)
    except Exception as err:  # the lever already applied; report the sync, never undo the change
        return {"ok": False, "rows": len(rows), "error": f"{type(err).__name__}: {err}"[:200]}
    return {"ok": result.ok, "rows": len(rows), "added": result.added, "updated": result.updated,
            "failed": result.failed}
