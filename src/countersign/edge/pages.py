"""Public catalogue pages: the `link` and `image_link` of every GMC row.

These are the only routes at the edge that answer without a TAP signature. They
show what anyone may see in a shop window (title, variant, price, availability)
and nothing an agent could act on: no offer, no checkout, no order.

The colours, type and spacing come from V8's tokens (`edge/static/tokens.css`),
light and dark: the page inlines them, and the image draws in light colours with
a dark rule for devices that prefer it (a renderer without CSS still gets a whole
picture). The image cannot read CSS variables, so it takes the font stacks from
`tokens.font()`.
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
    display, sans = escape(tokens.font("display")), escape(tokens.font("sans"))
    cat = _category(product)
    seed = hash_int(product.sku)
    name, _, variant = product.title.rpartition(" ")
    initials = "".join(word[0] for word in name.split()[:2]).upper()
    rotate = seed % 19 - 9  # a slight tilt fixed per SKU, enough to tell neighbours apart in a grid
    return (
        '<svg xmlns="http://www.w3.org/2000/svg" width="600" height="600" viewBox="0 0 600 600" '
        f'role="img" aria-label="{escape(product.title)}">'
        f'<style>@media (prefers-color-scheme: dark) {{ .bg {{ fill: {dark["bg"]} }} .card {{ fill: {dark[cat]} }} '
        f'.mark {{ fill: {dark["cat-ink"]} }} .name {{ fill: {dark["ink"]} }} .variant {{ fill: {dark["ink-2"]} }} }}</style>'
        f'<rect class="bg" width="600" height="600" fill="{light["bg"]}"/>'
        f'<rect class="card" x="160" y="104" width="280" height="280" rx="40" fill="{light[cat]}" '
        f'transform="rotate({rotate} 300 244)"/>'
        f'<text class="mark" x="300" y="284" text-anchor="middle" font-family="{display}" '
        f'font-size="116" font-weight="600" fill="{light["cat-ink"]}">{escape(initials)}</text>'
        f'<text class="name" x="300" y="476" text-anchor="middle" font-family="{sans}" '
        f'font-size="34" font-weight="600" fill="{light["ink"]}">{escape(name)}</text>'
        f'<text class="variant" x="300" y="522" text-anchor="middle" font-family="{sans}" '
        f'font-size="26" fill="{light["ink-2"]}">{escape(variant)}</text>'
        "</svg>"
    )


# The page's own rules, after the inlined tokens: no colour, face, size or radius of its own.
# Spacing follows the relationship: tight inside the title group, wide between groups.
_PAGE_CSS = """*, *::before, *::after { box-sizing: border-box; }
html, body { overflow-x: clip; }
body { margin: 0; background: var(--bg); color: var(--ink); font: var(--step-0)/1.55 var(--font-sans);
       -webkit-text-size-adjust: 100%; text-size-adjust: 100%; }
.shell { max-width: var(--container); margin: 0 auto; padding: 0 var(--space-5) var(--space-8); }
.mast { padding: var(--space-5) 0 var(--space-4); border-bottom: 1px solid var(--soft); }
.shop { font-family: var(--font-display); font-size: var(--step-1); color: var(--ink); }
.product { display: grid; grid-template-columns: minmax(0, 1fr); gap: var(--space-6); padding-top: var(--space-6); }
@media (min-width: 760px) {
  .product { grid-template-columns: minmax(0, 1fr) minmax(0, 1fr); gap: var(--space-7); align-items: start; }
}
.shot { display: block; width: 100%; height: auto; aspect-ratio: 1; border: 1px solid var(--soft);
        border-radius: var(--radius-2); }
h1 { font-family: var(--font-display); font-size: var(--step-4); font-weight: 600; line-height: 1.12;
     color: var(--ink); margin: 0 0 var(--space-2); text-wrap: balance; overflow-wrap: break-word; min-width: 0; }
.price { font-size: var(--step-2); font-weight: 600; font-variant-numeric: tabular-nums lining-nums;
         margin: 0 0 var(--space-3); }
.stock { display: inline-block; margin: 0; padding: var(--space-1) var(--space-3); border-radius: var(--radius-1);
         font-size: var(--step--1); font-weight: 600; }
.stock.in { color: var(--go); background: var(--go-bg); }
.stock.out { color: var(--info); background: var(--info-bg); }
.about { max-width: var(--measure); margin: var(--space-6) 0; text-wrap: pretty; }
.facts { margin: 0; border-top: 1px solid var(--soft); }
.facts div { display: grid; grid-template-columns: 7.5em minmax(0, 1fr); gap: var(--space-4);
             padding: var(--space-3) 0; border-bottom: 1px solid var(--soft); }
.facts dt { font-size: var(--step--1); color: var(--ink-2); padding-top: 0.15em; }
.facts dd { margin: 0; overflow-wrap: anywhere; }
.code { font-family: var(--font-mono); font-size: var(--step--1); padding-top: 0.15em; }
a { color: var(--accent); text-underline-offset: 0.2em; }
a:hover { text-decoration-thickness: 2px; }
a:active { color: var(--ink); }
a:focus-visible, [tabindex]:focus-visible { outline: 2px solid var(--accent); outline-offset: 3px;
                                           border-radius: var(--radius-1); }
@media (prefers-reduced-motion: reduce) {
  *, *::before, *::after { animation-duration: 0.01ms !important; transition-duration: 0.01ms !important; }
}
"""


def product_page(product: Product, currency: str = "USD", merchant_name: str = MERCHANT_NAME) -> str:
    in_stock = product.stock > 0
    variant = product.title.rsplit(" ", 1)[-1]
    sku, title, shop = escape(product.sku), escape(product.title), escape(merchant_name)
    about = escape(description(product, merchant_name))
    return f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{title} | {shop}</title>
<meta name="description" content="{about}">
<meta name="color-scheme" content="light dark">
<style>
{tokens.css()}{_PAGE_CSS}</style></head>
<body><div class="shell">
<header class="mast"><span class="shop">{shop}</span></header>
<main class="product">
<img class="shot" src="/img/{sku}.svg" alt="{title}" width="600" height="600">
<div class="details">
<h1>{title}</h1>
<p class="price">{escape(gmc_price(product.price, currency))}</p>
<p class="stock {"in" if in_stock else "out"}">{"In stock" if in_stock else "Out of stock"}</p>
<p class="about">{about}</p>
<dl class="facts">
<div><dt>Variant</dt><dd>{escape(variant)}</dd></div>
<div><dt>Range</dt><dd>{escape(product.category)}</dd></div>
<div><dt>Delivery</dt><dd>{escape(product.delivery)}</dd></div>
<div><dt>Sold by</dt><dd>{shop}</dd></div>
<div><dt>SKU</dt><dd class="code">{sku}</dd></div>
</dl>
</div>
</main>
</div></body></html>
"""
