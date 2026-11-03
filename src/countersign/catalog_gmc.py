"""C11: the merchant's catalogue in Google Merchant Center format, for Airwallex.

Airwallex's merchant MCP shows shopping agents the products we upload as GMC
rows (POST /api/v1/pa/agentic_commerce/products, body {"type": "gmc",
"gmc_products": [...]}, at most 300 rows a call). Every row is a product this
merchant can actually sell: a row Airwallex shows that offer() refuses would let
an agent find something the checkout then turns down. So the rows come from the
same catalogue the merchant sells from, and nothing is padded in.

Two rules from Airwallex's onboarding docs shape the upload:
- an upsert replaces the whole stored product, variants included, so the
  variants of one product (one item_group_id) always travel in the same call;
  split across two calls, the second would wipe the variants the first sent;
- there is no idempotency replay and failed rows do not roll back, so a re-run
  must be harmless by construction: the same rows give the same catalogue.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any, Callable, Iterable

from .merchant import MERCHANT_NAME, Product, synthetic_catalog
from .terms import minor_units, money

PRODUCTS_PATH = "/pa/agentic_commerce/products"  # under /api/v1, which the client adds
MAX_ROWS_PER_CALL = 300

# The fields Airwallex's own example row uses. Anything else is sent only on
# request (extended=True), until Airwallex confirms it accepts more of GMC.
DOCUMENTED_FIELDS = ("id", "title", "description", "link", "image_link", "availability", "price", "item_group_id")
EXTENDED_FIELDS = ("brand", "condition", "product_type", "identifier_exists")


def group_id(sku: str) -> str:
    """The product a variant belongs to: the SKU without its variant suffix (CAT-123-2 -> CAT-123)."""
    head, sep, _ = sku.rpartition("-")
    if not sep or not head:
        raise ValueError(f"{sku!r} has no variant suffix")
    return head


def gmc_price(amount: Decimal, currency: str) -> str:
    """GMC's money string, e.g. "51.99 USD", at the currency's own minor unit."""
    places = minor_units(currency)
    return f"{money(amount, currency):.{places}f} {currency.upper()}"


def description(product: Product, merchant_name: str = MERCHANT_NAME) -> str:
    """Deterministic: the same product always reads the same, so re-uploads change nothing."""
    variant = product.title.rsplit(" ", 1)[-1]
    return (f"{product.title}, the {variant} variant, from the {product.category} range at {merchant_name}. "
            f"Delivery: {product.delivery}.")


def _base(base_url: str) -> str:
    base = base_url.rstrip("/")
    if not base.startswith(("https://", "http://")):
        raise ValueError("base_url must be an absolute http(s) URL")
    return base


def to_gmc(product: Product, *, base_url: str, merchant_name: str = MERCHANT_NAME,
           currency: str = "USD", extended: bool = False) -> dict[str, str]:
    base = _base(base_url)
    row = {
        "id": product.sku,
        "title": product.title,
        "description": description(product, merchant_name),
        "link": f"{base}/p/{product.sku}",
        "image_link": f"{base}/img/{product.sku}.svg",
        "availability": "in_stock" if product.stock > 0 else "out_of_stock",
        "price": gmc_price(product.price, currency),
        "item_group_id": group_id(product.sku),
    }
    if extended:
        row.update({
            "brand": merchant_name,
            "condition": "new",
            "product_type": product.category,
            # Synthetic goods have no GTIN or MPN; say so rather than invent one.
            "identifier_exists": "no",
        })
    return row


def catalog(base_url: str, *, products: Iterable[Product] | None = None, merchant_name: str = MERCHANT_NAME,
            currency: str = "USD", extended: bool = False) -> list[dict[str, str]]:
    """One row per product the merchant sells, in catalogue order."""
    source = synthetic_catalog() if products is None else products
    return [to_gmc(p, base_url=base_url, merchant_name=merchant_name, currency=currency, extended=extended)
            for p in source]


def chunks(rows: list[dict[str, str]], limit: int = MAX_ROWS_PER_CALL) -> list[list[dict[str, str]]]:
    """Calls of at most `limit` rows, never splitting one product's variants across two calls."""
    groups: dict[str, list[dict[str, str]]] = {}
    for row in rows:
        groups.setdefault(row["item_group_id"], []).append(row)
    out: list[list[dict[str, str]]] = []
    current: list[dict[str, str]] = []
    for gid, members in groups.items():
        if len(members) > limit:
            raise ValueError(f"product {gid} has {len(members)} variants, more than one call can carry ({limit})")
        if len(current) + len(members) > limit:
            out.append(current)
            current = []
        current.extend(members)
    if current:
        out.append(current)
    return out


@dataclass
class UploadResult:
    added: int = 0
    updated: int = 0
    failed: int = 0
    calls: list[dict[str, int]] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return self.failed == 0


def upload(rows: list[dict[str, str]], post: Callable[[str, dict[str, Any]], dict[str, Any]], *,
           limit: int = MAX_ROWS_PER_CALL, pause: float = 0.15,
           sleep: Callable[[float], None] = time.sleep) -> UploadResult:
    """Upsert every row. `post(path, body)` is the Airwallex client's post.

    Upserts only: this never uses the file-ingest "replace" mode, which deletes
    every product absent from the file. The pause keeps us well under Airwallex's
    10 requests a second per endpoint.
    """
    result = UploadResult()
    for i, chunk in enumerate(chunks(rows, limit)):
        if i:
            sleep(pause)
        reply = post(PRODUCTS_PATH, {"type": "gmc", "gmc_products": chunk})
        counts = {k: int(reply.get(f"records_{k}") or 0) for k in ("added", "updated", "failed")}
        counts["rows"] = len(chunk)
        result.calls.append(counts)
        result.added += counts["added"]
        result.updated += counts["updated"]
        result.failed += counts["failed"]
    return result


def lookup(merchant: object) -> Callable[[str], Product | None]:
    """Live product data for the public product pages.

    A merchant that keeps its catalogue in memory (FakeMerchant's `catalog`) is
    read live, so a price or stock change shows on the page at once; anything
    else falls back to the published synthetic catalogue.
    """
    static = {p.sku: p for p in synthetic_catalog()}

    def find(sku: str) -> Product | None:
        live = getattr(merchant, "catalog", None)
        if isinstance(live, dict):
            return live.get(sku)
        return static.get(sku)

    return find
