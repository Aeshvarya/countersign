"""C8: the countersigned receipt.

When the merchant charges, it signs one small document that ties together
everything the charge rested on:

  - which agent asked (TAP key id, directory, nonce, tag)
  - what the shopper approved (the approval hash, the ceiling, who, when, and,
    with U1, the shopper's own passkey signature over it: C13)
  - what was charged (amount and currency, the locked rate and its quote id)
  - what the merchant owes (the order number and payment id)

Anyone with the merchant's public key can check it later, offline: a shopper,
their bank, a dispute team. "My agent bought this without asking" becomes a
question with a checkable answer: did the shopper approve terms with this hash,
was the charge at or under the ceiling, and did a known agent sign the request?
"""

from __future__ import annotations

import base64
import json
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from typing import Any

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey, Ed25519PublicKey

from .decide import Decision
from .merchant import Order
from .tap import AgentIdentity
from .terms import Approval, Terms, utc

VERSION = 1


def _canonical(body: dict[str, Any]) -> bytes:
    return json.dumps(body, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")


def _d(value: Decimal | None) -> str | None:
    return None if value is None else format(value, "f")


@dataclass(frozen=True)
class MerchantSigner:
    key: Ed25519PrivateKey
    key_id: str


def build(
    *,
    approval: Approval,
    live: Terms,
    decision: Decision,
    order: Order,
    agent: AgentIdentity | None,
    decided_at: datetime,
) -> dict[str, Any]:
    body = {
        "v": VERSION,
        "merchant_id": live.merchant_id,
        "order_number": order.order_number,
        "payment_id": order.payment_id,
        "idempotency_key": order.idempotency_key,
        "agent": None
        if agent is None
        else {"signature_agent": agent.signature_agent, "key_id": agent.key_id, "nonce": agent.nonce, "tag": agent.tag},
        "approval": {
            "hash": approval.hash,
            "ceiling": _d(approval.ceiling),
            "currency": approval.terms.shopper_currency,
            "approver": approval.approver,
            "approved_at": utc(approval.approved_at).isoformat(),
            "expires_at": utc(approval.expires_at).isoformat(),
        },
        "charged": {
            "amount": _d(decision.charge),
            "currency": decision.currency,
            "merchant_amount": _d(live.merchant_total),
            "merchant_currency": live.merchant_currency,
            "fx_quote_id": decision.fx_quote_id,
            "rate": _d(live.fx.rate) if live.fx else None,
        },
        "items": [{"sku": i.sku, "quantity": i.quantity, "unit_price": _d(i.unit_price)} for i in live.items],
        "delivery": live.delivery,
        "decided_at": utc(decided_at).isoformat(),
    }
    if approval.shopper_assertion is not None:
        # C13, additive: a receipt without it is byte for byte what it was before.
        body["approval"]["shopper_assertion"] = dict(approval.shopper_assertion)
    return body


def sign(body: dict[str, Any], signer: MerchantSigner) -> dict[str, Any]:
    raw = signer.key.sign(_canonical(body))
    return {"body": body, "key_id": signer.key_id, "alg": "ed25519", "signature": base64.b64encode(raw).decode()}


@dataclass(frozen=True)
class Check:
    ok: bool
    findings: tuple[str, ...]


def verify(receipt: dict[str, Any], merchant_key: Ed25519PublicKey, approval: Approval | None = None) -> Check:
    """Check a receipt. With the shopper's own copy of the approval, also check
    that the receipt is about that approval and stayed within its ceiling."""
    findings: list[str] = []
    try:
        merchant_key.verify(base64.b64decode(receipt["signature"]), _canonical(receipt["body"]))
        findings.append("signed by the merchant's key")
    except (InvalidSignature, KeyError, ValueError):
        return Check(False, ("the merchant's signature does not match this receipt",))

    body = receipt["body"]
    charged, approved = body["charged"], body["approval"]
    if charged["currency"] != approved["currency"]:
        return Check(False, (*findings, "charged in a different currency than approved"))
    if Decimal(charged["amount"]) > Decimal(approved["ceiling"]):
        return Check(False, (*findings, f"charged {charged['amount']} above the approved ceiling {approved['ceiling']}"))
    findings.append(f"charged {charged['amount']} {charged['currency']}, within the approved {approved['ceiling']}")

    shopper = approved.get("shopper_assertion")
    if shopper is not None:
        from .edge import passkey  # standard library and cryptography only

        result = passkey.verify_recorded(shopper, approved["hash"], approved["expires_at"])
        if isinstance(result, str):
            return Check(False, (*findings, f"the shopper's passkey signature does not check: {result}"))
        presence = "user verified" if result.user_verified else "user present"
        findings.append(f"the shopper's own passkey signed these exact terms at {shopper['origin']} ({presence})")

    if body["agent"] is None:
        findings.append("no agent identity recorded")
    else:
        findings.append(f"requested by agent key {body['agent']['key_id']} ({body['agent']['tag']})")

    if approval is not None:
        if not approval.is_intact():
            return Check(False, (*findings, "the shopper's copy of the approval does not match its own hash"))
        if approval.hash != approved["hash"]:
            return Check(False, (*findings, "this receipt is for a different approval than the shopper's copy"))
        findings.append("matches the shopper's own copy of the approval")
    return Check(True, tuple(findings))
