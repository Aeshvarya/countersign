"""V4′: upload the merchant's catalogue to Airwallex as GMC rows (C11).

    set -a; . ./.env; set +a
    PYTHONPATH=src python scripts/upload_catalog.py --dry-run
    PYTHONPATH=src python scripts/upload_catalog.py --base-url https://<public host>

Upserts only (POST /api/v1/pa/agentic_commerce/products). It never uses the
file-ingest "replace" mode, which deletes every product absent from the file.
Safe to re-run: the same rows give the same catalogue, and Airwallex counts a
repeat as an update. Exits 1 if any row failed, 2 if Airwallex refused the call.
Prints counts only: never a key, a token or a request header.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from typing import Callable, TextIO

from countersign import catalog_gmc
from countersign.airwallex import Airwallex, AirwallexError

DRY_RUN_BASE = "https://countersign.example"


def _refusal(err: AirwallexError) -> str:
    body = err.body if isinstance(err.body, dict) else {}
    parts = [f"HTTP {err.status}"] + [str(body[k]) for k in ("code", "message") if body.get(k)]
    return " ".join(parts)


def main(argv: list[str] | None = None, *, environ: dict[str, str] | None = None,
         client_factory: Callable[[], Airwallex] = Airwallex.from_env,
         out: TextIO = sys.stdout, sleep: Callable[[float], None] = time.sleep) -> int:
    env = os.environ if environ is None else environ
    parser = argparse.ArgumentParser(prog="upload_catalog.py", description=__doc__.split("\n\n")[0])
    parser.add_argument("--base-url", default=env.get("COUNTERSIGN_PUBLIC_URL"),
                        help="the public https host that serves /p/<sku> and /img/<sku>.svg")
    parser.add_argument("--merchant-name", default=catalog_gmc.MERCHANT_NAME)
    parser.add_argument("--extended", action="store_true",
                        help="also send brand, condition, product_type, identifier_exists")
    parser.add_argument("--limit", type=int, default=catalog_gmc.MAX_ROWS_PER_CALL)
    parser.add_argument("--dry-run", action="store_true", help="build and check the rows, call nothing")
    args = parser.parse_args(argv)

    have_keys = bool(env.get("AIRWALLEX_CLIENT_ID") and env.get("AIRWALLEX_API_KEY"))
    dry = args.dry_run or not have_keys
    if not 1 <= args.limit <= catalog_gmc.MAX_ROWS_PER_CALL:
        parser.error(f"--limit must be between 1 and {catalog_gmc.MAX_ROWS_PER_CALL}")
    if not dry and not (args.base_url or "").startswith("https://"):
        parser.error("a live upload needs --base-url (or COUNTERSIGN_PUBLIC_URL) as an https URL")

    rows = catalog_gmc.catalog(args.base_url or DRY_RUN_BASE, merchant_name=args.merchant_name,
                               extended=args.extended)
    calls = catalog_gmc.chunks(rows, args.limit)
    groups = len({r["item_group_id"] for r in rows})
    print(f"{len(rows)} rows, {groups} products, {len(calls)} call(s) of at most {args.limit} rows", file=out)

    if dry:
        if not args.dry_run:
            print("no Airwallex keys in the environment: dry run only", file=out)
        print("first row:", file=out)
        print(json.dumps(rows[0], indent=2), file=out)
        return 0

    client = client_factory()
    try:
        result = catalog_gmc.upload(rows, client.post, limit=args.limit, sleep=sleep)
    except AirwallexError as err:
        print(f"Airwallex refused the upload: {_refusal(err)}", file=out)
        return 2
    for n, call in enumerate(result.calls, 1):
        print(f"call {n}/{len(result.calls)}: {call['rows']} rows -> added {call['added']}, "
              f"updated {call['updated']}, failed {call['failed']}", file=out)
    print(f"total: added {result.added}, updated {result.updated}, failed {result.failed}", file=out)
    if not result.ok:
        print("some rows failed: the catalogue is partly updated (Airwallex does not roll back)", file=out)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
