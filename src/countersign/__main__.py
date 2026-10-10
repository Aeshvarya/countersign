"""Command line: `python -m countersign <command>`.

  keygen          print a fresh Ed25519 keypair (base64 raw, Visa's format)
  demo            run the contrast pairs and the dispute offline: no keys, no network
  fx              lock a real rate on the Airwallex sandbox (needs .env)
  verify-receipt  check a countersigned receipt: verify-receipt FILE --merchant-key B64 [--approval FILE]
"""

from __future__ import annotations

import argparse
import base64
import json
import sys
from datetime import datetime, timedelta, timezone
from decimal import Decimal

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

from . import receipt, tap, wire
from .checkout import MemoryReceiptStore, countersign
from .fx import FakeFx
from .merchant import FakeMerchant
from .terms import Approval


def _line(text: str = "") -> None:
    print(text)


def demo() -> int:
    now = datetime(2026, 11, 2, 10, 0, tzinfo=timezone.utc)
    fx = FakeFx("84.10")  # INR per USD
    shop = FakeMerchant(fx)
    product = shop.search("mechanical keyboard")[0]

    def approve():
        terms = shop.offer({product.sku: 1}, "INR", now)
        ceiling = (terms.shopper_total * Decimal("1.02")).quantize(Decimal("1"))  # "up to 2% more"
        return terms, Approval.grant(terms, ceiling, "shopper@example.com", now, now + timedelta(hours=2))

    _line(f"Product: {product.title} ({product.sku}), {product.price} USD")
    _line()
    _line("PAIR 1: same agent, same cart")
    terms, approval = approve()
    _line(f"  approved: {terms.shopper_total} INR, ceiling {approval.ceiling} INR")
    fx.rate = Decimal("84.90")  # the rate moves, still under the ceiling
    r = countersign(shop, approval, "order-1", now + timedelta(minutes=70))
    _line(f"  A  rate 84.10 → 84.90:  {r.decision.outcome.value.upper():8} {r.decision.charge} INR, order {r.order.order_number if r.order else '-'}")
    fx.rate = Decimal("84.10")
    terms, approval = approve()
    fx.rate = Decimal("88.40")  # the rate moves past the ceiling
    r = countersign(shop, approval, "order-2", now + timedelta(minutes=70))
    _line(f"  B  rate 84.10 → 88.40:  {r.decision.outcome.value.upper():8} {r.decision.reason}")
    for c in r.decision.changes:
        _line(f"       {c.field}: approved {c.approved}, now {c.live}")
    fx.rate = Decimal("84.10")
    terms, approval = approve()
    shop.set_delivery(product.sku, "standard, 12-15 business days")
    r = countersign(shop, approval, "order-3", now + timedelta(minutes=5))
    _line(f"  C  delivery slips:      {r.decision.outcome.value.upper():8} {r.decision.reason}")

    _line()
    _line("PAIR 2: same checkout, two callers")
    private_b64, public_b64 = tap.new_keypair()
    key = tap.load_private_key(private_b64)
    agent_dir = "https://agents.countersign.dev"
    directory = tap.StaticDirectory({(agent_dir, "agent-key-1"): key.public_key()})
    nonces = tap.MemoryNonceCache()
    signed = tap.sign(key, authority="shop.example", path="/checkout", key_id="agent-key-1",
                      signature_agent=agent_dir, tag=tap.PAY)
    ok = tap.verify(signed.headers(), authority="shop.example", path="/checkout", directory=directory, nonces=nonces)
    _line(f"  A  signed agent:        {'ACCEPTED' if isinstance(ok, tap.AgentIdentity) else 'REFUSED ' + ok.reason}")
    bot = tap.verify({}, authority="shop.example", path="/checkout", directory=directory, nonces=nonces)
    _line(f"  B  unsigned bot:        REFUSED  {bot.reason}")
    replay = tap.verify(signed.headers(), authority="shop.example", path="/checkout", directory=directory, nonces=nonces)
    _line(f"  C  replayed signature:  REFUSED  {replay.reason}")
    moved = tap.sign(key, authority="shop.example", path="/browse", key_id="agent-key-1",
                     signature_agent=agent_dir, tag=tap.PAY)
    other = tap.verify(moved.headers(), authority="shop.example", path="/checkout", directory=directory, nonces=nonces)
    _line(f"  D  signed for /browse:  REFUSED  {other.reason}")

    _line()
    _line("THE DISPUTE: three weeks later, \"my agent bought this without asking\"")
    merchant_key = tap.load_private_key(tap.new_keypair()[0])
    signer = receipt.MerchantSigner(merchant_key, "merchant-key-1")
    fx.rate = Decimal("84.10")
    terms, approval = approve()
    paid_by = tap.AgentIdentity(key_id="agent-key-1", signature_agent=agent_dir, tag=tap.PAY, nonce=ok.nonce)
    r = countersign(shop, approval, "order-4", now + timedelta(minutes=3), agent=paid_by, signer=signer, store=MemoryReceiptStore())
    check = receipt.verify(r.receipt, merchant_key.public_key(), approval)
    _line(f"  receipt for {r.order.order_number}: {'VERIFIED' if check.ok else 'FAILED'}")
    for finding in check.findings:
        _line(f"       {finding}")
    r.receipt["body"]["charged"]["amount"] = "1.00"
    _line(f"  same receipt, amount edited: {'VERIFIED' if receipt.verify(r.receipt, merchant_key.public_key()).ok else 'FAILED'}")
    return 0


def keygen() -> int:
    private, public = tap.new_keypair()
    print(f"TAP_PRIVATE_KEY_B64={private}")
    print(f"# public key (give this to the merchant's key directory): {public}")
    return 0


def fx_check(args: argparse.Namespace) -> int:
    from .airwallex import Airwallex
    from .fx import AirwallexFx

    gateway = AirwallexFx(Airwallex.from_env())
    print("shopper currencies:", ", ".join(gateway.shopper_currencies()))
    lock = gateway.lock(args.merchant, args.shopper, datetime.now(timezone.utc))
    print(f"{lock.quote_id}: 1 {lock.merchant_currency} = {lock.rate} {lock.shopper_currency}, until {lock.expires_at.isoformat()}")
    return 0


def verify_receipt(args: argparse.Namespace) -> int:
    with open(args.file) as f:
        signed = json.load(f)
    key = Ed25519PublicKey.from_public_bytes(base64.b64decode(args.merchant_key))
    approval = None
    if args.approval:
        with open(args.approval) as f:
            approval = wire.approval_from_json(json.load(f))
    check = receipt.verify(signed, key, approval)
    print("VERIFIED" if check.ok else "FAILED")
    for finding in check.findings:
        print(f"  {finding}")
    return 0 if check.ok else 1


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="countersign")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("demo")
    sub.add_parser("keygen")
    fx = sub.add_parser("fx")
    fx.add_argument("--merchant", default="USD")
    fx.add_argument("--shopper", default="INR")
    vr = sub.add_parser("verify-receipt")
    vr.add_argument("file")
    vr.add_argument("--merchant-key", required=True, help="base64 raw Ed25519 public key")
    vr.add_argument("--approval", help="the shopper's copy of the approval (JSON)")
    args = parser.parse_args(argv)
    if args.command == "demo":
        return demo()
    if args.command == "keygen":
        return keygen()
    if args.command == "verify-receipt":
        return verify_receipt(args)
    return fx_check(args)


if __name__ == "__main__":
    sys.exit(main())
