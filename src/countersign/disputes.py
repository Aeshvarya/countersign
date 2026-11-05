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

The builder guide's dispute kit opens a case the merchant will challenge at `RFI`; at
`CHARGEBACK` the challenge also needs a product type. Visa takes evidence as PDF, TIFF or
JPEG (a PDF at most 2 MB), not PNG, so the receipt goes in as a PDF written here with the
standard library: the signed JSON and what `receipt.verify` found in it.

`challenge_details` can also put the signed order into the challenge's own fields, so the
dispute record carries it field by field and not only inside the PDF. Which fields apply
depends on the stage (Airwallex's challenge reference, read 2026-11-06).
"""

from __future__ import annotations

import json
import re
import urllib.error
import urllib.parse
import urllib.request
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
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
                 stage: str = "RFI", due_in: timedelta = timedelta(days=7),
                 comment: str | None = None) -> Dispute:
    """The cardholder's bank opens a dispute on a paid intent (sandbox only).

    No amount: Airwallex then disputes the whole payment in the intent's own currency, so
    there is no currency here to get wrong.
    """
    body: dict[str, Any] = {"payment_intent_id": payment_intent_id, "stage": stage, "reason_code": reason_code,
                            "due_at": (now + due_in).astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S+0000")}
    if comment:
        body["comment"] = comment
    return _dispute(client.post("/simulation/pa/payment_disputes/create", body))


USER_AGENT = "countersign/1.0"


def files_url(base_url: str) -> str:
    """Airwallex serves uploads from its own host: api.sandbox.airwallex.com -> files.sandbox.airwallex.com."""
    scheme, sep, host = base_url.rstrip("/").partition("://")
    if not sep or not host.startswith("api."):
        raise ValueError(f"cannot tell Airwallex's file upload host from {base_url}")
    return f"{scheme}{sep}files.{host[len('api.'):]}"


def _field(text: str) -> str:
    """A multipart header value: no quote or line break can end it early."""
    return re.sub(r'["\r\n]', "", text)[:MAX_NAME]


def upload_file(client: Any, data: bytes, filename: str, notes: str = "", content_type: str = "application/pdf",
                opener: Callable[..., Any] = urllib.request.urlopen) -> str:
    """One file to Airwallex's file service; returns the file_id a challenge refers to.

    Its own request, not the client's: the client sends JSON to the API host only.
    """
    boundary = uuid.uuid4().hex
    filename, notes = _field(filename), _field(notes)
    parts = [f'--{boundary}\r\nContent-Disposition: form-data; name="file"; filename="{filename}"\r\n'
             f"Content-Type: {content_type}\r\n\r\n".encode() + data + b"\r\n"]
    if notes:
        parts.append(f'--{boundary}\r\nContent-Disposition: form-data; name="notes"\r\n\r\n{notes}\r\n'.encode())
    req = urllib.request.Request(f"{files_url(client.base_url)}/api/v1/files/upload",
                                 data=b"".join(parts) + f"--{boundary}--\r\n".encode(), method="POST")
    req.add_header("Authorization", f"Bearer {client.token()}")
    req.add_header("Content-Type", f"multipart/form-data; boundary={boundary}")
    # The file host's Cloudflare refuses Python's default User-Agent (403, error 1010; live 2026-11-05).
    req.add_header("User-Agent", USER_AGENT)
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


DETAIL_FIELDS = frozenset({"order_info", "seller_info", "delivery_info", "product_description",
                           "refund_refusal_reason"})  # all challenge_details may set; never the evidence or the ids


def challenge(client: Any, dispute: Dispute, file_id: str, *, description: str, product_description: str,
              request_id: str, details: dict[str, Any] | None = None) -> Dispute:
    """The merchant's answer: the receipt as the primary document, through the standard flow.

    `details` (from challenge_details) adds the order field by field; without it the body is
    the one that won live on 2026-11-05.
    """
    body: dict[str, Any] = {
        "request_id": request_id,
        "challenge_method": "STANDARD",
        "reason": "OTHER_REASONS",  # none of Airwallex's other reasons is "the payment was approved"
        "supporting_documents": {"documents": [{"type": "PRIMARY", "file_ids": [file_id],
                                                "description": description}]},
    }
    if dispute.stage == "CHARGEBACK":  # required there, and only applicable there
        body.update(product_type="PHYSICAL_GOODS", product_description=product_description)
    body.update({k: v for k, v in (details or {}).items() if k in DETAIL_FIELDS})
    return _dispute(client.post(f"/pa/payment_disputes/{dispute.id}/challenge", body))


def submit_evidence(client: Any, dispute: Dispute, pdf: bytes, *, filename: str, description: str,
                    product_description: str, details: dict[str, Any] | None = None,
                    opener: Callable[..., Any] = urllib.request.urlopen) -> Dispute:
    """Upload the receipt, then challenge the dispute with it."""
    if len(pdf) > MAX_PDF:
        raise ValueError(f"the evidence is {len(pdf)} bytes; Visa takes a PDF of at most {MAX_PDF}")
    file_id = upload_file(client, pdf, filename, notes=f"dispute {dispute.id}", opener=opener)
    return challenge(client, dispute, file_id, description=description,
                     product_description=product_description, request_id=client.request_id(), details=details)


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

    The JSON is the signed JSON re-indented, with anything outside ASCII written as a \\u
    escape (the standard fonts cannot draw it), so it parses back to the values that were
    signed. Long lines are cut, never re-flowed: joining them back gives the same text.
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
TEXT_WIDTH = 84  # Helvetica at 10 pt, with room for a run of capitals
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


# The order, field by field ------------------------------------------------------------------

MAX_SKU, MAX_DESC = 128, 500  # Airwallex's limits for a product's sku and desc
MAX_TEXT = 500  # Airwallex states none for product_description or refund_refusal_reason; kept to desc's


def _number(value: Any) -> float | None:
    """An amount as the challenge takes it: a JSON number, in major units like the dispute's own."""
    try:
        d = Decimal(str(value))
    except (InvalidOperation, ValueError):
        return None
    return float(d) if d.is_finite() else None


def challenge_details(signed: dict[str, Any], stage: str, *, store_url: str | None = None,
                      verify_url: str | None = None) -> dict[str, Any]:
    """The signed order, field by field, in the challenge sections Airwallex applies at this stage.

    Only what the receipt signed, plus the store's own address. The merchant holds no name,
    email or postal address for the shopper, so none is sent. At RFI: order_info, seller_info
    and delivery_info. At CHARGEBACK, where those do not apply: what was sold and why no
    refund is due. Any other stage: nothing. Not yet run against the sandbox (2026-11-06).
    """
    body = signed["body"]
    charged = body.get("charged") or {}
    amount, currency, merchant_currency = charged.get("amount"), charged.get("currency"), charged.get("merchant_currency")
    items = [i for i in body.get("items") or [] if isinstance(i, dict)]
    order = body.get("order_number")
    store = store_url.rstrip("/") if store_url else None

    if stage == "CHARGEBACK":
        sold = "; ".join(f"{i.get('quantity')} x {i.get('sku')} at {i.get('unit_price')} {merchant_currency}"
                         for i in items) or "see the receipt"
        reason = (f"The receipt the merchant signed for order {order} records this charge, {amount} {currency} "
                  f"({signed.get('alg')} key {signed.get('key_id')})")
        if verify_url:
            reason += f"; check it at {verify_url}"
        return {"product_description": f"{sold}; order {order}, charged {amount} {currency}"[:MAX_TEXT],
                "refund_refusal_reason": f"{reason}."[:MAX_TEXT]}
    if stage != "RFI":
        return {}

    products = []
    for item in items:
        sku = str(item.get("sku") or "")[:MAX_SKU]
        product: dict[str, Any] = {"sku": sku, "code": sku, "type": "physical_good"}
        if isinstance(item.get("quantity"), int) and not isinstance(item.get("quantity"), bool):
            product["quantity"] = item["quantity"]
        unit_price = _number(item.get("unit_price"))
        if currency == merchant_currency and unit_price is not None:
            product["unit_price"] = unit_price
        else:  # charged in another currency, and a product has no currency field: say it in words
            desc = f"{item.get('unit_price')} {merchant_currency} each"
            if charged.get("rate"):
                desc += f"; the order was charged in {currency} at {charged['rate']} {currency} per {merchant_currency}"
            product["desc"] = desc[:MAX_DESC]
        if store and sku:
            product["url"] = f"{store}/p/{urllib.parse.quote(sku, safe='')}"
        products.append(product)
    order_info: dict[str, Any] = {"id": order, "created_at": body.get("decided_at"), "products": products}
    if _number(amount) is not None and currency:
        order_info.update(total_amount=_number(amount), total_currency=currency)
    details: dict[str, Any] = {"order_info": {k: v for k, v in order_info.items() if v is not None}}
    if store:
        details["seller_info"] = {"store_url": store}
    if body.get("delivery"):
        details["delivery_info"] = {"shipping_method": str(body["delivery"])}
    return details
