"""U3: the dispute ending. Weeks after a purchase the cardholder says "my agent bought this
without asking", and the merchant answers with the receipt it signed when it charged.

Airwallex's sandbox plays the cardholder's bank: it opens the dispute on a paid intent and,
once the merchant has challenged it, resolves it. The resolution is a simulation; the
evidence is not: a receipt anyone can check offline with the merchant's public key.

The calls, checked against Airwallex's API reference and dispute guides on 2026-11-04:

    POST /api/v1/simulation/pa/payment_disputes/create        sandbox: the bank opens a dispute
    POST files.<env>.airwallex.com/api/v1/files/upload         multipart: the receipt as a file
    POST /api/v1/pa/payment_disputes/{id}/challenge            the merchant's answer, standard flow
    POST /api/v1/simulation/pa/payment_disputes/{id}/resolve   sandbox: decided for the merchant

Visa takes evidence as PDF, TIFF or JPEG (a PDF at most 2 MB), not PNG, so the receipt goes
in as a PDF written here with the standard library: the signed JSON exactly as signed, and
what `receipt.verify` found in it.
"""

from __future__ import annotations

import json
import re
import urllib.error
import urllib.request
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any, Callable

from .airwallex import AirwallexError

CARD_ABSENT_FRAUD = "10.4"  # Visa: "Other Fraud-Card Absent Environment", the "I never approved this" claim
MAX_PDF = 2 * 1024 * 1024  # Visa's limit for one PDF
MAX_NAME = 50  # the file service's limit, for the filename and for the notes alike


@dataclass(frozen=True)
class Dispute:
    id: str
    stage: str
    status: str  # REQUIRES_RESPONSE, CHALLENGED, then WON or REVERSED for the merchant, LOST for the shopper
    payment_intent_id: str | None


def _dispute(body: dict[str, Any]) -> Dispute:
    return Dispute(body["id"], body.get("stage", ""), body.get("status", ""), body.get("payment_intent_id"))


def open_dispute(client: Any, payment_intent_id: str, now: datetime, *, reason_code: str = CARD_ABSENT_FRAUD,
                 stage: str = "CHARGEBACK", due_in: timedelta = timedelta(days=7),
                 comment: str | None = None) -> Dispute:
    """The cardholder's bank opens a dispute on a paid intent (sandbox only).

    No amount: Airwallex then disputes the whole payment in the intent's own currency, so
    there is no currency here to get wrong.
    """
    body: dict[str, Any] = {"payment_intent_id": payment_intent_id, "stage": stage, "reason_code": reason_code,
                            "due_at": (now + due_in).strftime("%Y-%m-%dT%H:%M:%S+0000")}
    if comment:
        body["comment"] = comment
    return _dispute(client.post("/simulation/pa/payment_disputes/create", body))


def files_url(base_url: str) -> str:
    """Airwallex serves uploads from its own host: api.sandbox.airwallex.com -> files.sandbox.airwallex.com."""
    scheme, sep, host = base_url.rstrip("/").partition("://")
    if not sep or not host.startswith("api."):
        raise ValueError(f"cannot tell Airwallex's file upload host from {base_url}")
    return f"{scheme}{sep}files.{host[len('api.'):]}"


def upload_file(client: Any, data: bytes, filename: str, notes: str = "",
                opener: Callable[..., Any] = urllib.request.urlopen) -> str:
    """One file to Airwallex's file service; returns the file_id a challenge refers to."""
    boundary = uuid.uuid4().hex
    parts = [f'--{boundary}\r\nContent-Disposition: form-data; name="file"; filename="{filename}"\r\n'
             f"Content-Type: application/pdf\r\n\r\n".encode() + data + b"\r\n"]
    if notes:
        parts.append(f'--{boundary}\r\nContent-Disposition: form-data; name="notes"\r\n\r\n{notes}\r\n'.encode())
    req = urllib.request.Request(f"{files_url(client.base_url)}/api/v1/files/upload",
                                 data=b"".join(parts) + f"--{boundary}--\r\n".encode(), method="POST")
    req.add_header("Authorization", f"Bearer {client.token()}")
    req.add_header("Content-Type", f"multipart/form-data; boundary={boundary}")
    try:
        with opener(req, timeout=client.timeout) as resp:
            return json.loads(resp.read())["file_id"]
    except urllib.error.HTTPError as err:  # an answer, as the client reads one: before OSError
        raw = err.read().decode(errors="replace")
        try:
            body: dict | str = json.loads(raw)
        except json.JSONDecodeError:
            body = raw
        raise AirwallexError(err.code, body) from None


def challenge(client: Any, dispute_id: str, file_id: str, *, description: str, product_description: str,
              request_id: str) -> Dispute:
    """The merchant's answer: the receipt as the primary document, through the standard flow."""
    body = {
        "request_id": request_id,
        "challenge_method": "STANDARD",
        "reason": "OTHER_REASONS",  # none of Airwallex's other reasons is "the payment was approved"
        "product_type": "PHYSICAL_GOODS",  # required at CHARGEBACK
        "product_description": product_description,
        "supporting_documents": {"documents": [{"type": "PRIMARY", "file_ids": [file_id],
                                                "description": description}]},
    }
    return _dispute(client.post(f"/pa/payment_disputes/{dispute_id}/challenge", body))


def submit_evidence(client: Any, dispute_id: str, pdf: bytes, *, filename: str, description: str,
                    product_description: str, opener: Callable[..., Any] = urllib.request.urlopen) -> Dispute:
    """Upload the receipt, then challenge the dispute with it."""
    if len(pdf) > MAX_PDF:
        raise ValueError(f"the evidence is {len(pdf)} bytes; Visa takes a PDF of at most {MAX_PDF}")
    file_id = upload_file(client, pdf, filename, notes=f"dispute {dispute_id}"[:MAX_NAME], opener=opener)
    return challenge(client, dispute_id, file_id, description=description,
                     product_description=product_description, request_id=client.request_id())


def resolve_for_merchant(client: Any, dispute_id: str, comment: str | None = None) -> Dispute:
    """The bank decides for the merchant (sandbox only): the dispute ends WON or REVERSED."""
    body: dict[str, Any] = {"in_favor_of": "MERCHANT"}
    if comment:
        body["comment"] = comment
    return _dispute(client.post(f"/simulation/pa/payment_disputes/{dispute_id}/resolve", body))

