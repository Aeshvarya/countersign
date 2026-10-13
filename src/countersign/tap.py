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
import json
import re
import secrets
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from typing import Any, Callable, Protocol

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
    """Where the merchant looks up an agent's public key.

    None means the directory answered and has no such active key. A directory
    that cannot be asked raises DirectoryUnavailable instead.
    """

    def public_key(self, signature_agent: str, key_id: str) -> Ed25519PublicKey | None: ...


class DirectoryUnavailable(Exception):
    """The key directory could not be asked. The request is refused, never waved through."""


class NonceCache(Protocol):
    def seen(self, nonce: str, expires: int) -> bool:
        """Record the nonce; return True if it had already been used."""
        ...


class MemoryNonceCache:
    """Single-use nonces. Locked, because a server checks requests on parallel
    threads and two copies of one signed request must not both pass."""

    def __init__(self) -> None:
        self._nonces: dict[str, int] = {}
        self._lock = threading.Lock()

    def seen(self, nonce: str, expires: int) -> bool:
        now = int(time.time())
        with self._lock:
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


_KEY_ID = re.compile(r"^[A-Za-z0-9._-]{1,100}$")  # the key ids Visa's proxy accepts


class RegistryDirectory:
    """Agent keys from a registry speaking the API of Visa's reference
    agent-registry (github.com/visa/trusted-agent-protocol, agent-registry/main.py).

    A key counts only for the agent named in Signature-Agent. Key ids are not
    unique across agents (Visa's sample data gives two agents a "primary"), so
    the registry's list of active agents, GET /agents, is read for the exact
    (agent, key id) pair; that list also leaves out deactivated agents, which
    GET /keys/{key_id} does not. A registry without the list falls back to
    GET /keys/{key_id}, accepted only when its agent_domain is this agent.
    Active Ed25519 keys only.

    Answers are cached for `ttl` seconds: a new or revoked key takes effect
    within that time, and a flood of unknown key ids costs the registry nothing.
    """

    def __init__(self, base_url: str, ttl: float = 60, timeout: float = 3,
                 clock: Callable[[], float] = time.monotonic) -> None:
        self.base_url = base_url.rstrip("/")
        self.ttl = ttl
        self.timeout = timeout
        self.clock = clock
        self._lock = threading.Lock()
        self._agents: tuple[float, list | None] | None = None
        self._keys: dict[str, tuple[float, Any]] = {}

    def public_key(self, signature_agent: str, key_id: str) -> Ed25519PublicKey | None:
        if not _KEY_ID.match(key_id):
            return None
        agents = self._agent_list()
        if agents is not None:
            entry = next((k for a in agents if isinstance(a, dict) and a.get("domain") == signature_agent
                          and a.get("is_active") == "true"
                          for k in a.get("keys") or [] if isinstance(k, dict) and k.get("key_id") == key_id), None)
        else:
            entry = self._key(key_id)
            if not isinstance(entry, dict) or entry.get("agent_domain") != signature_agent:
                entry = None
        return _ed25519(entry)

    def _fresh(self, cached: tuple[float, Any] | None) -> bool:
        return cached is not None and self.clock() - cached[0] < self.ttl

    def _agent_list(self) -> list | None:
        """The active agents with their keys, or None if this registry has no list."""
        with self._lock:
            if self._fresh(self._agents):
                return self._agents[1]
        agents = self._get("/agents")
        if agents is not None and not isinstance(agents, list):
            raise DirectoryUnavailable("the registry's agent list is not a list")
        with self._lock:
            self._agents = (self.clock(), agents)
        return agents

    def _key(self, key_id: str) -> Any:
        with self._lock:
            cached = self._keys.get(key_id)
            if self._fresh(cached):
                return cached[1]
        entry = self._get(f"/keys/{urllib.parse.quote(key_id)}")
        with self._lock:
            if len(self._keys) >= 10_000:
                self._keys.clear()
            self._keys[key_id] = (self.clock(), entry)
        return entry

    def _get(self, path: str) -> Any:
        """The JSON at path, None on 404, DirectoryUnavailable for anything else."""
        try:
            with urllib.request.urlopen(self.base_url + path, timeout=self.timeout) as resp:
                return json.loads(resp.read())
        except urllib.error.HTTPError as err:
            if err.code == 404:
                return None
            raise DirectoryUnavailable(f"the registry answered HTTP {err.code}") from None
        except (OSError, ValueError) as err:  # refused, timed out, or not JSON
            raise DirectoryUnavailable(f"the registry could not be read ({type(err).__name__})") from None


def _ed25519(entry: Any) -> Ed25519PublicKey | None:
    if not isinstance(entry, dict) or entry.get("is_active") != "true":
        return None
    if str(entry.get("algorithm", "")).lower() != "ed25519":
        return None
    try:
        raw = base64.b64decode(str(entry.get("public_key", "")).strip(), validate=True)
    except ValueError:
        return None
    return Ed25519PublicKey.from_public_bytes(raw) if len(raw) == 32 else None


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

    try:
        public_key = directory.public_key(agent, key_id)
    except DirectoryUnavailable:
        return Rejection("the agent key directory could not be reached")
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
