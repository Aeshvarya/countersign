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


# The evidence file -------------------------------------------------------------------------

def evidence_filename(signed: dict[str, Any]) -> str:
    order = re.sub(r"[^A-Za-z0-9_-]", "", str(signed["body"].get("order_number") or "order"))
    return f"receipt-{order}"[: MAX_NAME - len(".pdf")] + ".pdf"


def evidence_lines(signed: dict[str, Any], findings: tuple[str, ...], public_key: str,
                   verify_url: str | None = None) -> list[tuple[str, str]]:
    """What the PDF says, as (font, text): facts read from the receipt, then the receipt itself.

    The JSON is printed exactly as signed (only re-indented), and long lines are cut, never
    re-flowed, so joining a cut line back gives the same characters.
    """
    body = signed["body"]
    lines = [
        ("title", "Countersign receipt"),
        ("text", f"Order {body.get('order_number')}, payment {body.get('payment_id')}, "
                 f"merchant {body.get('merchant_id')}"),
        ("text", f"Signed {body.get('decided_at')} with {signed.get('alg')} key {signed.get('key_id')}"),
        ("text", f"Merchant public key (base64): {public_key}"),
    ]
    if verify_url:
        lines.append(("text", f"Check it in a browser: {verify_url}"))
    lines += [("text", ""), ("bold", "receipt.verify found")]
    lines += [("text", f"- {f}") for f in findings]
    lines += [("text", ""), ("bold", "The signed receipt (JSON)")]
    for line in json.dumps(signed, indent=2, sort_keys=True).splitlines():
        lines += [("mono", line[i:i + MONO_WIDTH]) for i in range(0, max(len(line), 1), MONO_WIDTH)]
    return lines


# A one-file PDF writer: the three standard fonts, A4, top to bottom. No dependency.
FONTS = {"title": ("F2", 14), "bold": ("F2", 10), "text": ("F1", 10), "mono": ("F3", 8)}
MONO_WIDTH = 100  # Courier is 0.6 em wide: 100 characters at 8 pt fit the 495 pt line
TEXT_WIDTH = 92
PAGE, MARGIN = (595, 842), 50


def _wrap(font: str, text: str) -> list[str]:
    if font == "mono" or len(text) <= TEXT_WIDTH:
        return [text]
    out, line = [], ""
    for word in text.split(" "):
        while len(word) > TEXT_WIDTH:  # a key or an id with no spaces: cut it
            if line:
                out.append(line)
                line = ""
            out.append(word[:TEXT_WIDTH])
            word = word[TEXT_WIDTH:]
        if line and len(line) + 1 + len(word) > TEXT_WIDTH:
            out.append(line)
            line = word
        else:
            line = f"{line} {word}" if line else word
    return out + [line] if line else out


def _escape(text: str) -> bytes:
    raw = text.encode("cp1252", errors="replace")
    return raw.replace(b"\\", b"\\\\").replace(b"(", b"\\(").replace(b")", b"\\)")


def render_pdf(lines: list[tuple[str, str]]) -> bytes:
    pages: list[list[bytes]] = [[]]
    y = PAGE[1] - MARGIN
    for font, text in lines:
        name, size = FONTS[font]
        for part in _wrap(font, text):
            step = round(size * 1.4)
            if y - step < MARGIN:
                pages.append([])
                y = PAGE[1] - MARGIN
            y -= step
            pages[-1].append(b"BT /%s %d Tf %d %d Td (%s) Tj ET" % (name.encode(), size, MARGIN, y, _escape(part)))

    fonts = [b"Helvetica", b"Helvetica-Bold", b"Courier"]
    first_page = 3 + len(fonts)  # 1 catalog, 2 pages, then the fonts, then a page and its stream each
    kids = b" ".join(b"%d 0 R" % (first_page + 2 * i) for i in range(len(pages)))
    objects = [b"<< /Type /Catalog /Pages 2 0 R >>",
               b"<< /Type /Pages /Kids [%s] /Count %d >>" % (kids, len(pages))]
    objects += [b"<< /Type /Font /Subtype /Type1 /BaseFont /%s /Encoding /WinAnsiEncoding >>" % f for f in fonts]
    resources = b"<< /Font << /F1 3 0 R /F2 4 0 R /F3 5 0 R >> >>"
    for i, ops in enumerate(pages):
        stream = b"\n".join(ops)
        objects.append(b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 %d %d] /Resources %s /Contents %d 0 R >>"
                       % (PAGE[0], PAGE[1], resources, first_page + 2 * i + 1))
        objects.append(b"<< /Length %d >>\nstream\n%s\nendstream" % (len(stream), stream))

    out = bytearray(b"%PDF-1.4\n")
    offsets = []
    for n, obj in enumerate(objects, start=1):
        offsets.append(len(out))
        out += b"%d 0 obj\n%s\nendobj\n" % (n, obj)
    xref = len(out)
    out += b"xref\n0 %d\n0000000000 65535 f \n" % (len(objects) + 1)
    out += b"".join(b"%010d 00000 n \n" % o for o in offsets)
    out += b"trailer\n<< /Size %d /Root 1 0 R >>\nstartxref\n%d\n%%%%EOF\n" % (len(objects) + 1, xref)
    return bytes(out)


def evidence_pdf(signed: dict[str, Any], findings: tuple[str, ...], public_key: str,
                 verify_url: str | None = None) -> bytes:
    return render_pdf(evidence_lines(signed, findings, public_key, verify_url))


def evidence_description(signed: dict[str, Any], findings: tuple[str, ...]) -> str:
    """The challenge's one line about the file: what was checked, in receipt.verify's own words."""
    return (f"Merchant-signed receipt for order {signed['body'].get('order_number')}. "
            f"receipt.verify: {'; '.join(findings)}.")


def product_description(signed: dict[str, Any]) -> str:
    items = signed["body"].get("items") or []
    return ", ".join(f"{i.get('quantity')} x {i.get('sku')}" for i in items) or "see the receipt"
