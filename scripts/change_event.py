"""V6: change a product on the running host, on cue, for the demo and the video.

    set -a; . ./.env; set +a
    python scripts/change_event.py --sku KIT-123-0 --price 54.99
    python scripts/change_event.py --sku KIT-123-0 --percent 12          the price up 12 %
    python scripts/change_event.py --sku KIT-123-0 --stock 0
    python scripts/change_event.py --sku KIT-123-0 --delivery "express, 2 business days"
    python scripts/change_event.py --sku KIT-123-0 --reset              this product as uploaded
    python scripts/change_event.py --reset                              every moved product as uploaded

The house merchant keeps its catalogue in memory inside the host, so a change
goes through the host's lever routes (POST /api/levers/*, behind
COUNTERSIGN_LEVER_TOKEN): the same functions the merchant console calls. It
lands at once, so a shopper who approved the old terms is asked again and
nothing is charged. When the host runs with COUNTERSIGN_SYNC_CATALOG=1, the
product is also upserted to Airwallex whole, all its variants in one call, so
the merchant MCP's search shows the same thing. Delivery has no GMC shipping
field here: it travels in the row's description.

The token is read from the environment only, never from the command line.

Exit 0: changed, and Airwallex updated when the host syncs. 1: changed on the
host, but Airwallex's catalogue was NOT updated, so the two now differ (run it
again, or --reset). 2: nothing changed (refused, wrong token, host unreachable).
"""

from __future__ import annotations

import argparse
import os
import sys
from typing import Any, Callable, Mapping

from countersign import catalog_gmc

LEVERS = "/api/levers"


def request_for(args: argparse.Namespace) -> tuple[str, dict[str, Any]]:
    if args.reset:
        return f"{LEVERS}/reset", ({"sku": args.sku} if args.sku else {})
    if args.price is not None:
        return f"{LEVERS}/price", {"sku": args.sku, "price": args.price}
    if args.percent is not None:
        return f"{LEVERS}/price", {"sku": args.sku, "percent": args.percent}
    if args.stock is not None:
        return f"{LEVERS}/stock", {"sku": args.sku, "stock": args.stock}
    return f"{LEVERS}/delivery", {"sku": args.sku, "delivery": args.delivery}


def _error(response: Any) -> str:
    try:
        body = response.json()
    except ValueError:
        return response.text[:200]
    if isinstance(body, dict):
        return str(body.get("error") or body.get("detail") or body)[:200]
    return str(body)[:200]


def apply(client: Any, token: str, path: str, body: dict[str, Any], out: Callable[[str], None] = print) -> int:
    try:
        response = client.post(path, json=body, headers={"Authorization": f"Bearer {token}"})
    except Exception as err:  # the host never answered, so nothing can have changed
        out(f"NOT CHANGED: the host did not answer ({type(err).__name__})")
        return 2
    if response.status_code != 200:
        out(f"NOT CHANGED: {response.status_code} {_error(response)}")
        return 2
    data = response.json()
    changes = data["changes"] if "changes" in data else [data]
    if not changes:
        out("Nothing to reset: every product is as uploaded.")
        return 0
    for c in changes:
        out(f"CHANGED  {c['sku']} {c['field']}: {c['before']} → {c['after']}  ({c['title']})")
    syncs: dict[str, dict[str, Any] | None] = {}
    for c in changes:
        syncs.setdefault(catalog_gmc.group_id(c["sku"]), c.get("sync"))
    code = 0
    for group, sync in syncs.items():
        if sync is None:
            out(f"Airwallex not asked for {group}: the host runs without COUNTERSIGN_SYNC_CATALOG=1, "
                "so the merchant MCP still shows the old values")
        elif sync.get("ok"):
            out(f"Airwallex catalogue updated for {group}: {sync.get('rows', 0)} rows")
        else:
            why = sync.get("error") or f"{sync.get('failed', 0)} rows failed"
            out(f"Airwallex catalogue NOT updated for {group} ({why}): "
                "the host has the change, Airwallex does not")
            code = 1
    return code


def main(argv: list[str] | None = None, *, environ: Mapping[str, str] | None = None, client: Any = None,
         out: Callable[[str], None] = print) -> int:
    environ = os.environ if environ is None else environ
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--base-url", default=environ.get("COUNTERSIGN_PUBLIC_URL"))
    parser.add_argument("--sku", help="the product id, e.g. KIT-123-0")
    what = parser.add_mutually_exclusive_group(required=True)
    what.add_argument("--price", help="the new price in the merchant's currency, e.g. 54.99")
    what.add_argument("--percent", help="move the price by this percent, e.g. 12 or -10")
    what.add_argument("--stock", type=int, help="units in stock; 0 makes it unavailable")
    what.add_argument("--delivery", help="the delivery promise, e.g. \"express, 2 business days\"")
    what.add_argument("--reset", action="store_true", help="put the product (or, with no --sku, all) back")
    args = parser.parse_args(argv)
    if not args.reset and not args.sku:
        parser.error("--sku is required, except with --reset")
    token = environ.get("COUNTERSIGN_LEVER_TOKEN", "").strip()
    if not token:
        parser.error("COUNTERSIGN_LEVER_TOKEN is not set: it is the host's lever token")
    path, body = request_for(args)
    if client is not None:
        return apply(client, token, path, body, out)
    if not args.base_url:
        parser.error("--base-url or COUNTERSIGN_PUBLIC_URL is required")
    import httpx

    with httpx.Client(base_url=args.base_url.rstrip("/"), timeout=60, follow_redirects=False) as http:
        return apply(http, token, path, body, out)


if __name__ == "__main__":
    sys.exit(main())
