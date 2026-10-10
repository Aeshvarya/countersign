"""C2: Visa Trusted Agent Protocol signatures, both directions.

The agent signs every request to the merchant; the merchant verifies before it
serves anything. The wire format follows Visa's reference implementation
(github.com/visa/trusted-agent-protocol, tap-agent and cdn-proxy) byte for byte,
so a signature made here verifies there and the other way round:

  Signature-Agent: "<agent directory URL>"
  Signature-Input: sig2=("@authority" "@path"); created=<unix>; expires=<unix>;
                   keyId="<key id>"; alg="ed25519"; nonce="<nonce>"; tag="<tag>"
  Signature:       sig2=:<base64 Ed25519 signature>:

Signature base (three lines joined by "\\n"):

  "@authority": <host>
  "@path": <path>
  "@signature-params": <Signature-Input without the "sig2=" prefix>

Visa's sample departs from strict RFC 9421 in small ways ("; " separators,
"keyId", the label "sig2"). Matching the sample is the point: interop with the
reference verifier is what makes this TAP rather than TAP-shaped.
"""

from __future__ import annotations

import base64
import re
import secrets
import time
from dataclasses import dataclass
from typing import Protocol

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey, Ed25519PublicKey

BROWSE = "agent-browser-auth"
PAY = "agent-payer-auth"
TAGS = (BROWSE, PAY)

_INPUT = re.compile(r'^sig2=\((?P<components>[^)]*)\);\s*(?P<attrs>.+)$')
_ATTR = re.compile(r'(\w+)=("[^"]*"|\d+)')
_SIG = re.compile(r"^sig2=:(?P<b64>[A-Za-z0-9+/=]+):$")


@dataclass(frozen=True)
class Signed:
    signature_agent: str
    signature_input: str
    signature: str

    def headers(self) -> dict[str, str]:
        return {
            "Signature-Agent": f'"{self.signature_agent}"',
            "Signature-Input": self.signature_input,
            "Signature": self.signature,
        }


def signature_params(created: int, expires: int, key_id: str, nonce: str, tag: str) -> str:
    return (
        f'("@authority" "@path"); created={created}; expires={expires}; '
        f'keyId="{key_id}"; alg="ed25519"; nonce="{nonce}"; tag="{tag}"'
    )


def signature_base(authority: str, path: str, params: str) -> str:
    return "\n".join(
        [
            f'"@authority": {authority}',
            f'"@path": {path}',
            f'"@signature-params": {params}',
        ]
    )


def sign(
    key: Ed25519PrivateKey,
    *,
    authority: str,
    path: str,
    key_id: str,
    signature_agent: str,
    tag: str,
    now: int | None = None,
    ttl: int = 480,
    nonce: str | None = None,
) -> Signed:
    if tag not in TAGS:
        raise ValueError(f"tag must be one of {TAGS}")
    created = int(time.time()) if now is None else now
    params = signature_params(created, created + ttl, key_id, nonce or secrets.token_urlsafe(16), tag)
    raw = key.sign(signature_base(authority, path, params).encode("utf-8"))
    return Signed(signature_agent, f"sig2={params}", f"sig2=:{base64.b64encode(raw).decode()}:")


# ----------------------------------------------------------------- verifying


@dataclass(frozen=True)
class AgentIdentity:
    key_id: str
    signature_agent: str
    tag: str
    nonce: str


@dataclass(frozen=True)
class Rejection:
    reason: str


class KeyDirectory(Protocol):
    """Where the merchant looks up an agent's public key."""

    def public_key(self, signature_agent: str, key_id: str) -> Ed25519PublicKey | None: ...


class NonceCache(Protocol):
    def seen(self, nonce: str, expires: int) -> bool:
        """Record the nonce; return True if it had already been used."""
        ...


class MemoryNonceCache:
    def __init__(self) -> None:
        self._nonces: dict[str, int] = {}

    def seen(self, nonce: str, expires: int) -> bool:
        now = int(time.time())
        self._nonces = {n: e for n, e in self._nonces.items() if e >= now}
        if nonce in self._nonces:
            return True
        self._nonces[nonce] = expires
        return False


class StaticDirectory:
    """A fixed key list, for tests and for running without Visa's registry."""

    def __init__(self, keys: dict[tuple[str, str], Ed25519PublicKey]) -> None:
        self._keys = keys

    def public_key(self, signature_agent: str, key_id: str) -> Ed25519PublicKey | None:
        return self._keys.get((signature_agent, key_id))


def parse_input(signature_input: str) -> tuple[list[str], dict[str, str | int]] | None:
    match = _INPUT.match(signature_input.strip())
    if not match:
        return None
    components = [c.strip('"') for c in match["components"].split()]
    attrs: dict[str, str | int] = {}
    for key, value in _ATTR.findall(match["attrs"]):
        attrs[key] = value[1:-1] if value.startswith('"') else int(value)
    return components, attrs


def verify(
    headers: dict[str, str],
    *,
    authority: str,
    path: str,
    directory: KeyDirectory,
    nonces: NonceCache,
    require_tag: str | None = None,
    now: int | None = None,
    skew: int = 60,
) -> AgentIdentity | Rejection:
    """Check one request. Every failure says why, so the refusal can be shown."""
    lower = {k.lower(): v for k, v in headers.items()}
    agent = lower.get("signature-agent", "").strip().strip('"')
    signature_input = lower.get("signature-input", "")
    signature = lower.get("signature", "")
    if not (agent and signature_input and signature):
        return Rejection("unsigned request: no Signature-Agent, Signature-Input or Signature header")

    parsed = parse_input(signature_input)
    sig = _SIG.match(signature.strip())
    if parsed is None or sig is None:
        return Rejection("the signature headers are not in TAP format")
    components, attrs = parsed
    if components != ["@authority", "@path"]:
        return Rejection("the signature must cover @authority and @path")
    if attrs.get("alg") != "ed25519":
        return Rejection("only ed25519 signatures are accepted")
    tag, nonce, key_id = attrs.get("tag"), attrs.get("nonce"), attrs.get("keyId")
    created, expires = attrs.get("created"), attrs.get("expires")
    if tag not in TAGS:
        return Rejection("unknown tag")
    if require_tag and tag != require_tag:
        return Rejection(f"this action is tagged {tag}; it needs {require_tag}")
    if not (isinstance(nonce, str) and nonce and isinstance(key_id, str) and key_id):
        return Rejection("the signature has no nonce or key id")
    if not (isinstance(created, int) and isinstance(expires, int)):
        return Rejection("the signature has no validity window")

    clock = int(time.time()) if now is None else now
    if created > clock + skew:
        return Rejection("the signature was created in the future")
    if expires < clock:
        return Rejection("the signature has expired")

    public_key = directory.public_key(agent, key_id)
    if public_key is None:
        return Rejection("the agent's key is not in the directory")

    params = signature_input.strip()[len("sig2="):]
    try:
        public_key.verify(base64.b64decode(sig["b64"]), signature_base(authority, path, params).encode("utf-8"))
    except (InvalidSignature, ValueError):
        return Rejection("the signature does not match this merchant and page")

    # Replay check last, so a forged request cannot burn a real agent's nonce.
    if nonces.seen(nonce, expires):
        return Rejection("this signature has already been used")

    return AgentIdentity(key_id=key_id, signature_agent=agent, tag=tag, nonce=nonce)


def load_private_key(b64: str) -> Ed25519PrivateKey:
    return Ed25519PrivateKey.from_private_bytes(base64.b64decode(b64))


def new_keypair() -> tuple[str, str]:
    """(private, public), each base64 of the raw 32 bytes, Visa's format."""
    from cryptography.hazmat.primitives import serialization

    key = Ed25519PrivateKey.generate()
    private = key.private_bytes(serialization.Encoding.Raw, serialization.PrivateFormat.Raw, serialization.NoEncryption())
    public = key.public_key().public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)
    return base64.b64encode(private).decode(), base64.b64encode(public).decode()
