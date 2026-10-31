"""U1 / C13: the shopper's own passkey signature over the exact terms.

The approval page asks the shopper's device for a WebAuthn assertion
(`navigator.credentials.get()`) whose challenge is `approval_challenge(...)`.
`verify_assertion` checks what comes back with the standard library and
`cryptography` only: no WebAuthn library and no CBOR parser, because the page
sends the SPKI DER public key that `PublicKeyCredential.getPublicKey()` returns
at registration.

The challenge binds three things, not just the terms hash:
  - the terms hash (C1: the bound fields and the ceiling)
  - the checkout session id, so a signature captured from one session cannot
    approve a second session with an identical cart (a second charge)
  - the approval's expiry, so the shopper signs "these terms, until then"
It is a hash, so the browser signs 32 bytes, and anyone holding a receipt can
recompute it offline from the receipt alone.

Honest limit: a passkey proves that this device approved these terms. It is
not legal identity, and in the demo the shopper is synthetic.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import hmac
import json
import string
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Mapping

from cryptography.exceptions import InvalidSignature, UnsupportedAlgorithm
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives.serialization import load_der_public_key

from ..terms import utc

CHALLENGE_LABEL = "countersign-u1-v1"
FLAG_USER_PRESENT = 0x01
FLAG_USER_VERIFIED = 0x04
_B64URL = frozenset(string.ascii_letters + string.digits + "-_")


def b64url(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def b64url_decode(value: Any) -> bytes:
    """Strict base64url: the URL-safe alphabet only, padding optional. ValueError otherwise."""
    if not isinstance(value, str):
        raise ValueError("not a string")
    text = value.rstrip("=")
    if not text or not set(text) <= _B64URL:
        raise ValueError("not base64url")
    try:
        return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))
    except binascii.Error:
        raise ValueError("not base64url") from None


def approval_challenge(session_id: str, terms_hash: str, expires_at: datetime) -> bytes:
    """The 32 bytes the shopper's passkey signs for one session's exact terms."""
    material = "\n".join([CHALLENGE_LABEL, session_id, terms_hash, utc(expires_at).isoformat()])
    return hashlib.sha256(material.encode("utf-8")).digest()


@dataclass(frozen=True)
class Verified:
    user_verified: bool  # recorded, not required: the user-present flag is what is checked
    sign_count: int


def verify_assertion(*, public_key_spki_der: bytes, authenticator_data: bytes, client_data_json: bytes,
                     signature: bytes, expected_challenge: bytes, origin: str, rp_id: str) -> Verified | str:
    """Verified, or the reason the assertion is refused. The checks run in this order:

    1. clientDataJSON is a JSON object and its type is "webauthn.get" (an
       assertion, not a registration).
    2. Its challenge decodes to exactly the expected challenge: these terms, this
       session, this expiry.
    3. Its origin is exactly ours, and the call was not made from a cross-origin
       frame: a page on another site cannot borrow the shopper's passkey for us.
    4. authenticatorData starts with SHA-256 of our RP id: the credential is scoped
       to our site, not another one.
    5. The user-present flag is set: a person touched the device.
    6. The key is ECDSA P-256 and the signature (ES256, DER) verifies over
       authenticatorData || SHA-256(clientDataJSON).

    Checks 1 to 5 read fields that only the signature in check 6 makes
    trustworthy, so all six must pass; a field that passes before check 6 proves
    nothing on its own. clientDataJSON is parsed from, and hashed as, the exact
    bytes received: never re-serialised, or the hash would not be what was signed.
    """
    # 1
    try:
        client = json.loads(client_data_json.decode("utf-8"))
    except (UnicodeDecodeError, ValueError):
        return "clientDataJSON is not valid JSON"
    if not isinstance(client, dict):
        return "clientDataJSON is not a JSON object"
    if client.get("type") != "webauthn.get":
        return "clientDataJSON type is not webauthn.get"
    # 2
    try:
        challenge = b64url_decode(client.get("challenge"))
    except ValueError:
        return "the challenge is missing or not base64url"
    if not hmac.compare_digest(challenge, expected_challenge):
        return "the challenge is not for these terms"
    # 3
    if client.get("origin") != origin:
        return "the origin does not match"
    if client.get("crossOrigin") is True:
        return "the assertion was made in a cross-origin frame"
    # 4
    if len(authenticator_data) < 37:
        return "authenticatorData is too short"
    if not hmac.compare_digest(authenticator_data[:32], hashlib.sha256(rp_id.encode("utf-8")).digest()):
        return "the RP id hash does not match"
    # 5
    flags = authenticator_data[32]
    if not flags & FLAG_USER_PRESENT:
        return "the user-present flag is not set"
    # 6
    try:
        key = load_der_public_key(public_key_spki_der)
    except (ValueError, UnsupportedAlgorithm):
        return "the public key is not valid SPKI DER"
    if not isinstance(key, ec.EllipticCurvePublicKey) or not isinstance(key.curve, ec.SECP256R1):
        return "the public key is not ECDSA P-256"
    signed = authenticator_data + hashlib.sha256(client_data_json).digest()
    try:
        key.verify(signature, signed, ec.ECDSA(hashes.SHA256()))
    except InvalidSignature:
        return "the signature does not verify"
    return Verified(user_verified=bool(flags & FLAG_USER_VERIFIED),
                    sign_count=int.from_bytes(authenticator_data[33:37], "big"))


def verify_recorded(record: Mapping[str, Any], terms_hash: str, expires_at: str) -> Verified | str:
    """Check a shopper signature as a receipt records it (C13), offline.

    The challenge is recomputed from the receipt itself: the recorded session id,
    the approval hash and the approval's expiry. The origin and RP id are read
    from the record and reported, so whoever checks can see which site the shopper
    signed on; the public key is what "this device" means. The merchant's
    signature over the whole receipt is what stops any of these being swapped.
    """
    try:
        raw = {name: b64url_decode(record[name])
               for name in ("public_key_spki_der", "authenticator_data", "client_data_json", "signature")}
        session_id, origin, rp_id = record["session_id"], record["origin"], record["rp_id"]
        if not all(isinstance(v, str) and v for v in (session_id, origin, rp_id)):
            raise ValueError("session_id, origin and rp_id must be strings")
        challenge = approval_challenge(session_id, terms_hash, datetime.fromisoformat(expires_at))
    except (KeyError, TypeError, ValueError):
        return "the recorded signature is incomplete"
    return verify_assertion(**raw, expected_challenge=challenge, origin=origin, rp_id=rp_id)
