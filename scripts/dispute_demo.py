"""U3: the dispute ending, on Airwallex's sandbox, answered with the signed receipt.

    set -a; . ./.env; set +a
    python scripts/dispute_demo.py --receipt receipt.json
    python scripts/dispute_demo.py --receipt receipt.json --intent int_...   the intent, if you name it, must be the receipt's
    python scripts/dispute_demo.py --receipt receipt.json --merchant-key <base64>          offline key, no host
    python scripts/dispute_demo.py --receipt receipt.json --stage RFI                     opens earlier; the sandbox cannot decide it
    python scripts/dispute_demo.py --receipt receipt.json --structured                    also the order field by field (not yet run live)

The receipt is the signed JSON the shopper page keeps (body, key_id, alg, signature).

1. The cardholder's bank opens a dispute on the paid intent: Airwallex's simulation API,
   Visa reason 10.4 (card-absent fraud), the whole payment, at CHARGEBACK. At RFI the
   sandbox takes the challenge but refuses the resolve ("Dispute transition is not
   supported", live 2026-11-05); from CHARGEBACK the live run ended WON.
2. receipt.verify checks the receipt against the merchant's public key, read from the host
   (COUNTERSIGN_PUBLIC_URL + /.well-known/countersign-merchant-key) or given with
   --merchant-key. If it does not verify, the script stops: the merchant has no answer, and
   the dispute stays open.
3. The receipt goes up as a PDF and the dispute is challenged with it. With --structured
   the challenge also carries the signed order in Airwallex's own fields for that stage
   (disputes.challenge_details); the line names the fields it added.
4. The bank decides for the merchant (simulation API).

Prints the dispute id and its status after each step, and what receipt.verify found. Never
a header, a token, or a value from Airwallex's bodies.

Exit 0: decided for the merchant. 1: the receipt does not verify, or is for another
payment. 2: Airwallex refused a call, could not be reached, or did not decide for the
merchant; the reason is printed.
"""

from __future__ import annotations

import argparse
import base64
import json
import os
import sys
import urllib.request
from datetime import datetime, timezone
from typing import Any, Callable, Mapping

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

from countersign import disputes, receipt
from countersign.airwallex import UNREACHABLE, Airwallex, AirwallexError

KEY_PATH = "/.well-known/countersign-merchant-key"


def host_key(base_url: str) -> str:
    """The merchant's public key as the host publishes it (base64 of the raw 32 bytes)."""
    with urllib.request.urlopen(f"{base_url.rstrip('/')}{KEY_PATH}", timeout=60) as resp:
        return json.loads(resp.read())["public_key"]


def run(client: Any, signed: dict[str, Any], public_key: str, now: datetime, *, intent: str, stage: str,
        verify_url: str | None, out: Callable[[str], None], opener: Callable[..., Any] | None = None,
        store_url: str | None = None, structured: bool = False) -> int:
    try:
        key = Ed25519PublicKey.from_public_bytes(base64.b64decode(public_key, validate=True))
    except (ValueError, TypeError):
        out("  stopped: the merchant key is not base64 of a 32-byte Ed25519 public key")
        return 1
    step = "open"
    try:
        opened = disputes.open_dispute(client, intent, now, stage=stage)
        out(f"1 dispute opened   {opened.id}  {opened.stage} {opened.status}")

        check = receipt.verify(signed, key)
        out(f"2 receipt          {'VERIFIED' if check.ok else 'FAILED'}")
        for finding in check.findings:
            out(f"    {finding}")
        if not check.ok:
            out(f"  stopped: the receipt does not verify, so there is no answer to give; {opened.id} stays open")
            return 1

        step = "evidence"
        pdf = disputes.evidence_pdf(signed, check.findings, public_key, verify_url)
        extra = {} if opener is None else {"opener": opener}
        details = (disputes.challenge_details(signed, opened.stage, store_url=store_url, verify_url=verify_url)
                   if structured else None)
        answered = disputes.submit_evidence(client, opened, pdf, filename=disputes.evidence_filename(signed),
                                            description=disputes.evidence_description(signed, check.findings),
                                            product_description=disputes.product_description(signed),
                                            details=details, **extra)
        added = f", plus {', '.join(sorted(details))}" if details else ""
        out(f"3 evidence sent    {answered.id}  {answered.stage} {answered.status}  (receipt PDF, {len(pdf)} bytes{added})")

        step = "resolve"
        resolved = disputes.resolve_for_merchant(client, opened.id)
        out(f"4 decided          {resolved.id}  {resolved.stage} {resolved.status}")
    except AirwallexError as err:
        code = err.body.get("code") if isinstance(err.body, dict) else None
        out(f"  stopped at {step}: Airwallex answered HTTP {err.status}{f' {code}' if code else ''}")
        return 2
    except UNREACHABLE as err:
        out(f"  stopped at {step}: Airwallex could not be reached ({type(err).__name__})")
        return 2
    if resolved.status not in ("WON", "REVERSED"):
        out(f"  stopped: the dispute ended {resolved.status or 'with no status'}, not for the merchant")
        return 2
    return 0


def main(argv: list[str] | None = None, *, environ: Mapping[str, str] | None = None, client: Any = None,
         fetch_key: Callable[[str], str] = host_key, opener: Callable[..., Any] | None = None,
         now: datetime | None = None, out: Callable[[str], None] = print) -> int:
    environ = os.environ if environ is None else environ
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--receipt", required=True, help="the signed receipt, as JSON")
    parser.add_argument("--intent", help="the paid payment intent; defaults to the receipt's payment_id")
    parser.add_argument("--merchant-key", help="base64 of the merchant's Ed25519 public key, instead of the host's")
    parser.add_argument("--base-url", default=environ.get("COUNTERSIGN_PUBLIC_URL"), help="the host, for its key")
    parser.add_argument("--stage", choices=("RFI", "CHARGEBACK"), default="CHARGEBACK", help="where the dispute opens")
    parser.add_argument("--structured", action="store_true",
                        help="also send the signed order in the challenge's own fields (not yet run live)")
    args = parser.parse_args(argv)

    with open(args.receipt) as fh:
        signed = json.load(fh)
    body = signed.get("body") if isinstance(signed, dict) else None
    paid = body.get("payment_id") if isinstance(body, dict) else None
    intent = args.intent or paid
    if not intent:
        out("  stopped: the receipt names no payment, so there is no intent to dispute")
        return 1
    if paid != intent:
        # Checked before anything opens: a dispute on one payment answered with another's receipt proves nothing.
        out(f"  stopped: the receipt is for payment {paid}, not {intent}")
        return 1

    if args.merchant_key:
        public_key = args.merchant_key
    elif args.base_url:
        try:
            public_key = fetch_key(args.base_url)
        except (*UNREACHABLE, KeyError, TypeError, ValueError) as err:  # down, or not answering with a key
            out(f"  stopped: the host's merchant key could not be read ({type(err).__name__})")
            return 2
    else:
        parser.error("--merchant-key, --base-url or COUNTERSIGN_PUBLIC_URL is required")
    store_url = args.base_url.rstrip("/") if args.base_url else None
    verify_url = f"{store_url}/verify" if store_url else None

    if client is None:
        try:
            client = Airwallex.from_env()
        except RuntimeError as err:
            parser.error(str(err))
    return run(client, signed, public_key, now or datetime.now(timezone.utc), intent=intent, stage=args.stage,
               verify_url=verify_url, out=out, opener=opener, store_url=store_url, structured=args.structured)


if __name__ == "__main__":
    sys.exit(main())
