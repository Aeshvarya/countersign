"""Shared-secret HMAC checks for calls that come from a platform, not an agent.

Airwallex calls the merchant's `create_checkout` URL signed with an HMAC-SHA256
secret set at onboarding (C10), and signs its webhooks the same way. Airwallex
has not published the exact header names or the signed string for
create_checkout, so the scheme is chosen by configuration, one at a time, never
"accept any":

- "airwallex" (default): Airwallex's own webhook convention. `x-timestamp` in
  milliseconds, `x-signature` = hex HMAC-SHA256 over timestamp + raw body.
- "acp": the Agentic Commerce Protocol's webhook format. `Merchant-Signature:
  t=<unix seconds>,v1=<hex>` over t + "." + raw body.
- "guess": `Timestamp` (seconds) + `Signature` = base64 HMAC over ts + "." + body.

Every scheme checks the raw bytes as received, before any JSON parsing, with a
constant-time compare and a window of five minutes either side.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import time
from typing import Mapping

SCHEMES = ("airwallex", "acp", "guess")
WINDOW_SECONDS = 300


def _lower(headers: Mapping[str, str]) -> dict[str, str]:
    return {k.lower(): v for k, v in headers.items()}


def _mac(secret: bytes, message: bytes) -> bytes:
    return hmac.new(secret, message, hashlib.sha256).digest()


def _within(ts_seconds: float, now: float) -> str | None:
    if ts_seconds > now + WINDOW_SECONDS:
        return "the signature is dated in the future"
    if ts_seconds < now - WINDOW_SECONDS:
        return "the signature is too old"
    return None


def _int(value: str | None) -> int | None:
    # ASCII digits only: str.isdigit() also passes "²", which int() then rejects with a ValueError.
    if value is None or not (value.strip().isascii() and value.strip().isdigit()):
        return None
    return int(value.strip())


def _same(expected: str, given: str) -> bool:
    # compare_digest raises on a str with non-ASCII characters; a header can carry any.
    return given.isascii() and hmac.compare_digest(expected, given)


def verify(headers: Mapping[str, str], raw_body: bytes, secret: bytes, *, scheme: str = "airwallex",
           now: float | None = None) -> str | None:
    """None if the call is authentic, otherwise the reason it is refused."""
    if scheme not in SCHEMES:
        raise ValueError(f"unknown HMAC scheme {scheme!r}")
    if not secret:
        return "no shared secret is configured"
    h = _lower(headers)
    clock = time.time() if now is None else now

    if scheme == "airwallex":
        ts, sig = h.get("x-timestamp"), h.get("x-signature")
        ms = _int(ts)
        if ms is None or not sig:
            return "missing x-timestamp or x-signature"
        if ms < 10**12:  # a plausible date in seconds, not milliseconds
            return "x-timestamp is not in milliseconds"
        late = _within(ms / 1000, clock)
        if late:
            return late
        expected = _mac(secret, ts.strip().encode() + raw_body).hex()
        return None if _same(expected, sig.strip().lower()) else "the signature does not match"

    if scheme == "acp":
        header = h.get("merchant-signature")
        if not header:
            return "missing Merchant-Signature"
        parts = dict(p.strip().split("=", 1) for p in header.split(",") if "=" in p)
        t, v1 = _int(parts.get("t")), parts.get("v1")
        if t is None or not v1:
            return "Merchant-Signature needs t and v1"
        late = _within(t, clock)
        if late:
            return late
        expected = _mac(secret, f"{t}.".encode() + raw_body).hex()
        return None if _same(expected, v1.strip().lower()) else "the signature does not match"

    ts, sig = h.get("timestamp"), h.get("signature")
    t = _int(ts)
    if t is None or not sig:
        return "missing Timestamp or Signature"
    late = _within(t, clock)
    if late:
        return late
    expected = base64.b64encode(_mac(secret, f"{t}.".encode() + raw_body)).decode()
    return None if _same(expected, sig.strip()) else "the signature does not match"


def sign(raw_body: bytes, secret: bytes, *, scheme: str = "airwallex", now: float | None = None) -> dict[str, str]:
    """Headers for a call signed under `scheme`. For tests and our own fallback door."""
    clock = time.time() if now is None else now
    if scheme == "airwallex":
        ts = str(int(clock * 1000))
        return {"x-timestamp": ts, "x-signature": _mac(secret, ts.encode() + raw_body).hex()}
    if scheme == "acp":
        t = int(clock)
        return {"Merchant-Signature": f"t={t},v1={_mac(secret, f'{t}.'.encode() + raw_body).hex()}"}
    if scheme == "guess":
        t = int(clock)
        return {"Timestamp": str(t), "Signature": base64.b64encode(_mac(secret, f"{t}.".encode() + raw_body)).decode()}
    raise ValueError(f"unknown HMAC scheme {scheme!r}")
