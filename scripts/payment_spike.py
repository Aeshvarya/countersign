"""A2 + A6 in one run against the Airwallex sandbox: real rate lock, real payment.

    set -a; . ./.env; set +a
    python scripts/payment_spike.py --shopper INR

1. Lists the shopper currencies this sandbox can convert to, and locks a
   USD -> shopper rate with Multi-Currency Pricing.
2. Builds a cart, an approval with a ceiling, and runs the countersign decision.
3. Creates a payment intent in USD and serves a local page with Airwallex's
   Drop-in element, given the locked quote. Pay with the sandbox test card
   4035501000000008, any future expiry, any CVC.
4. Fetches the intent again and checks it against the countersign: status,
   amount and currency (the shopper's), order number, approval hash, rate lock.

Prints what Airwallex returned at each step, so the rounding question (does
their shopper amount equal ours?) is answered by data.
"""

from __future__ import annotations

import argparse
import json
import sys
import threading
import time
import uuid
import webbrowser
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from http.server import BaseHTTPRequestHandler, HTTPServer

from countersign.airwallex import Airwallex
from countersign.decide import decide
from countersign.fx import AirwallexFx
from countersign.merchant import FakeMerchant
from countersign.payments import TEST_CARD, AirwallexPayments, check_paid
from countersign.terms import Approval

PAGE = """<!doctype html><html><head><meta charset="utf-8"><title>Countersign payment spike</title>
<script src="https://static.airwallex.com/components/sdk/v1/index.js"></script>
<style>body{font-family:system-ui;max-width:560px;margin:40px auto;padding:0 16px}h1{font-size:22px}</style></head>
<body><h1>Pay %(total)s %(ccy)s (locked rate)</h1>
<p>Sandbox test card: <b>%(card)s</b>, any future expiry, any CVC.</p><div id="dropIn"></div><p id="out"></p>
<script>(async () => {
  await window.AirwallexComponentsSDK.init({ env: 'demo', enabledElements: ['payments'] });
  const el = await window.AirwallexComponentsSDK.createElement('dropIn', %(options)s);
  el.mount('dropIn');
  el.on('success', async () => { document.getElementById('out').textContent = 'Paid. Checking server-side…'; await fetch('/done', {method: 'POST'}); });
  el.on('error', (e) => { document.getElementById('out').textContent = 'Error: ' + e.detail.error.message; });
})();</script></body></html>"""


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--shopper", default="INR")
    parser.add_argument("--port", type=int, default=8790)
    parser.add_argument("--no-browser", action="store_true")
    args = parser.parse_args()

    client = Airwallex.from_env()
    fx = AirwallexFx(client)
    currencies = fx.shopper_currencies()
    print("1. convertible shopper currencies:", ", ".join(currencies))
    if args.shopper not in currencies:
        print(f"   {args.shopper} is not convertible on this sandbox; pick one of the above with --shopper")
        return 1

    now = datetime.now(timezone.utc)
    merchant = FakeMerchant(fx)
    sku = merchant.search("mechanical keyboard")[0].sku
    terms = merchant.offer({sku: 1}, args.shopper, now)
    print(f"   locked {terms.fx.quote_id}: 1 USD = {terms.fx.rate} {args.shopper} until {terms.fx.expires_at.isoformat()}")
    print(f"   offer: {terms.merchant_total} USD -> {terms.shopper_total} {args.shopper} (our rounding)")

    approval = Approval.grant(terms, (terms.shopper_total * Decimal("1.02")).quantize(Decimal("1")),
                              "spike@countersign.dev", now, now + timedelta(minutes=30))
    decision = decide(approval, terms, now)
    print(f"2. countersign: {decision.outcome.value} — {decision.reason}")

    order_number = f"ORD-SPIKE-{uuid.uuid4().hex[:6].upper()}"
    key = str(uuid.uuid4())
    payments = AirwallexPayments(client)
    handoff = payments.start(decision, merchant_total=terms.merchant_total, merchant_currency="USD",
                             order_number=order_number, approval_hash=approval.hash, idempotency_key=key)
    print(f"3. payment intent {handoff.intent_id} created in USD for {order_number}")

    done = threading.Event()
    options = json.dumps({"intent_id": handoff.intent_id, "client_secret": handoff.client_secret,
                          "quote_id": handoff.quote_id, "currency": handoff.currency})

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def do_GET(self):
            page = PAGE % {"total": terms.shopper_total, "ccy": args.shopper, "card": TEST_CARD, "options": options}
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.end_headers()
            self.wfile.write(page.encode())

        def do_POST(self):
            done.set()
            self.send_response(204)
            self.end_headers()

    server = HTTPServer(("127.0.0.1", args.port), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    url = f"http://127.0.0.1:{args.port}/"
    print(f"   pay here: {url}")
    if not args.no_browser:
        webbrowser.open(url)
    done.wait(timeout=600)
    time.sleep(1)
    server.shutdown()

    intent = payments.retrieve(handoff.intent_id)
    print(f"4. Airwallex says: {intent.status}, {intent.amount} {intent.currency}, quote {intent.conversion_quote_id}")
    print(f"   our shopper total: {decision.charge} {decision.currency}")
    check = check_paid(intent, decision, order_number=order_number, approval_hash=approval.hash)
    print("   " + ("PAID AS APPROVED" if check.ok else "MISMATCH"))
    for reason in check.reasons:
        print(f"   - {reason}")
    return 0 if check.ok else 1


if __name__ == "__main__":
    sys.exit(main())
