"""Public catalogue pages: the `link` and `image_link` of every GMC row.

These are the only routes at the edge that answer without a TAP signature. They
show what anyone may see in a shop window (title, variant, price, availability)
and nothing an agent could act on: no offer, no checkout, no order.

The colours are one table so V8's palette can replace them in one place.
"""

from __future__ import annotations

import re
from html import escape

from ..catalog_gmc import MERCHANT_NAME, description, gmc_price
from ..merchant import Product, hash_int

# Neutral until V8 lands: background, surface, ink, muted ink, accent per category.
PALETTE = {
    "bg": "#f4f4f2",
    "surface": "#ffffff",
    "ink": "#1d1d1b",
    "muted": "#6b6b66",
    "accents": ("#3d5a80", "#5b7f5a", "#8c5a3c", "#6d597a", "#4a6c6f"),
}

# Exactly the paths these pages answer, so the TAP exemption can never widen by accident.
_SKU = r"[A-Za-z0-9]{1,16}(?:-[A-Za-z0-9]{1,16}){1,3}"
PAGE_PATH = re.compile(rf"^/p/({_SKU})$")
IMAGE_PATH = re.compile(rf"^/img/({_SKU})\.svg$")


def is_public(method: str, path: str) -> bool:
    return method in ("GET", "HEAD") and bool(PAGE_PATH.match(path) or IMAGE_PATH.match(path))


def _accent(product: Product) -> str:
    accents = PALETTE["accents"]
    return accents[hash_int(product.category) % len(accents)]


def product_svg(product: Product) -> str:
    """A deterministic placeholder image: the same SKU always draws the same picture."""
    accent = _accent(product)
    seed = hash_int(product.sku)
    name, _, variant = product.title.rpartition(" ")
    initials = "".join(word[0] for word in name.split()[:2]).upper()
    rotate = seed % 30 - 15
    return (
        '<svg xmlns="http://www.w3.org/2000/svg" width="600" height="600" viewBox="0 0 600 600" '
        f'role="img" aria-label="{escape(product.title)}">'
        f'<rect width="600" height="600" fill="{PALETTE["bg"]}"/>'
        f'<rect x="150" y="110" width="300" height="300" rx="48" fill="{accent}" '
        f'transform="rotate({rotate} 300 260)"/>'
        f'<text x="300" y="290" text-anchor="middle" font-family="Helvetica, Arial, sans-serif" '
        f'font-size="120" font-weight="700" fill="{PALETTE["surface"]}">{escape(initials)}</text>'
        f'<text x="300" y="490" text-anchor="middle" font-family="Helvetica, Arial, sans-serif" '
        f'font-size="34" fill="{PALETTE["ink"]}">{escape(name)}</text>'
        f'<text x="300" y="535" text-anchor="middle" font-family="Helvetica, Arial, sans-serif" '
        f'font-size="26" fill="{PALETTE["muted"]}">{escape(variant)}</text>'
        "</svg>"
    )


def product_page(product: Product, currency: str = "USD", merchant_name: str = MERCHANT_NAME) -> str:
    in_stock = product.stock > 0
    return f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{escape(product.title)} | {escape(merchant_name)}</title>
<style>
body {{ margin: 0; background: {PALETTE["bg"]}; color: {PALETTE["ink"]}; font: 16px/1.5 Helvetica, Arial, sans-serif; }}
main {{ max-width: 720px; margin: 40px auto; padding: 24px; background: {PALETTE["surface"]}; border-radius: 16px; }}
img {{ width: 100%; max-width: 360px; display: block; margin: 0 auto 16px; }}
.price {{ font-size: 28px; font-weight: 700; }}
.muted {{ color: {PALETTE["muted"]}; }}
</style></head>
<body><main>
<img src="/img/{escape(product.sku)}.svg" alt="{escape(product.title)}">
<h1>{escape(product.title)}</h1>
<p class="price">{escape(gmc_price(product.price, currency))}</p>
<p>{"In stock" if in_stock else "Out of stock"}</p>
<p>{escape(description(product, merchant_name))}</p>
<p class="muted">SKU {escape(product.sku)} · sold by {escape(merchant_name)}</p>
</main></body></html>
"""
