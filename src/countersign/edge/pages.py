"""Public catalogue pages: the `link` and `image_link` of every GMC row.

These are the only routes at the edge that answer without a TAP signature. They
show what anyone may see in a shop window (title, variant, price, availability)
and nothing an agent could act on: no offer, no checkout, no order.

The colours come from V8's tokens (`edge/static/tokens.css`), light and dark:
the page inlines them, and the image draws in light colours with a dark rule
for devices that prefer it (a renderer without CSS still gets a whole picture).
"""

from __future__ import annotations

import re
from html import escape

from ..catalog_gmc import MERCHANT_NAME, description, gmc_price
from ..merchant import Product, hash_int
from . import tokens


# Exactly the paths these pages answer, so the TAP exemption can never widen by accident.
_SKU = r"[A-Za-z0-9]{1,16}(?:-[A-Za-z0-9]{1,16}){1,3}"
PAGE_PATH = re.compile(rf"^/p/({_SKU})$")
IMAGE_PATH = re.compile(rf"^/img/({_SKU})\.svg$")


def is_public(method: str, path: str) -> bool:
    return method in ("GET", "HEAD") and bool(PAGE_PATH.match(path) or IMAGE_PATH.match(path))


def _category(product: Product) -> str:
    """The token role of this product's category colour."""
    roles = tokens.categories()
    return roles[hash_int(product.category) % len(roles)]


def product_svg(product: Product) -> str:
    """A deterministic placeholder image: the same SKU always draws the same picture."""
    light, dark = tokens.themes()["light"], tokens.themes()["dark"]
    cat = _category(product)
    seed = hash_int(product.sku)
    name, _, variant = product.title.rpartition(" ")
    initials = "".join(word[0] for word in name.split()[:2]).upper()
    rotate = seed % 30 - 15
    return (
        '<svg xmlns="http://www.w3.org/2000/svg" width="600" height="600" viewBox="0 0 600 600" '
        f'role="img" aria-label="{escape(product.title)}">'
        f'<style>@media (prefers-color-scheme: dark) {{ .bg {{ fill: {dark["bg"]} }} .card {{ fill: {dark[cat]} }} '
        f'.mark {{ fill: {dark["cat-ink"]} }} .name {{ fill: {dark["ink"]} }} .variant {{ fill: {dark["ink-2"]} }} }}</style>'
        f'<rect class="bg" width="600" height="600" fill="{light["bg"]}"/>'
        f'<rect class="card" x="150" y="110" width="300" height="300" rx="48" fill="{light[cat]}" '
        f'transform="rotate({rotate} 300 260)"/>'
        f'<text class="mark" x="300" y="290" text-anchor="middle" font-family="Helvetica, Arial, sans-serif" '
        f'font-size="120" font-weight="700" fill="{light["cat-ink"]}">{escape(initials)}</text>'
        f'<text class="name" x="300" y="490" text-anchor="middle" font-family="Helvetica, Arial, sans-serif" '
        f'font-size="34" fill="{light["ink"]}">{escape(name)}</text>'
        f'<text class="variant" x="300" y="535" text-anchor="middle" font-family="Helvetica, Arial, sans-serif" '
        f'font-size="26" fill="{light["ink-2"]}">{escape(variant)}</text>'
        "</svg>"
    )


def product_page(product: Product, currency: str = "USD", merchant_name: str = MERCHANT_NAME) -> str:
    in_stock = product.stock > 0
    return f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{escape(product.title)} | {escape(merchant_name)}</title>
<meta name="color-scheme" content="light dark">
<style>
{tokens.css()}body {{ margin: 0; background: var(--bg); color: var(--ink); font: 16px/1.5 Helvetica, Arial, sans-serif; }}
main {{ max-width: 720px; margin: 40px auto; padding: 24px; background: var(--panel); border-radius: 16px; }}
img {{ width: 100%; max-width: 360px; display: block; margin: 0 auto 16px; }}
.price {{ font-size: 28px; font-weight: 700; }}
.muted {{ color: var(--ink-2); }}
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
