"""How the agent reaches a merchant: always through a TAP-signed request.

`ShopClient` is what the agent's tools call. Two implementations:

- `LocalShop` runs the merchant in-process (FakeMerchant, or any MerchantGateway)
  but still signs every call and verifies it at a simulated edge, so TAP is
  exercised on every request even offline.
- `HttpShop` speaks the C7 HTTP API to a real merchant edge (WORKPLAN V2).

The TAP path that is signed and verified is the request path including its
query string, as in Visa's reference proxy (`req.url`).
"""

from __future__ import annotations

import json
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Callable, Protocol

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from .. import receipt as receipts
from .. import tap, wire
from ..checkout import MemoryReceiptStore, Result, countersign
from ..decide import Decision, Outcome
from ..merchant import MerchantGateway, Product
from ..terms import Approval, Terms


@dataclass(frozen=True)
class AgentKey:
    key: Ed25519PrivateKey
    key_id: str
    signature_agent: str

    def sign(self, authority: str, path: str, tag: str, body: bytes | None = None) -> dict[str, str]:
        return tap.sign(self.key, authority=authority, path=path, key_id=self.key_id,
                        signature_agent=self.signature_agent, tag=tag, body=body).headers()


class Refused(Exception):
    """The merchant's edge refused the request (TAP check failed)."""


class MerchantError(Exception):
    """The merchant answered with an error status other than a TAP refusal."""

    def __init__(self, status: int, payload: Any):
        self.status = status
        self.payload = payload
        super().__init__(f"merchant answered HTTP {status}: {payload}")


class ShopClient(Protocol):
    def search(self, query: str, limit: int) -> list[Product]: ...
    def offer(self, cart: dict[str, int], shopper_currency: str) -> Terms: ...
    def checkout(self, approval: Approval, idempotency_key: str) -> Result: ...


@dataclass
class LocalShop:
    merchant: MerchantGateway
    agent_key: AgentKey
    clock: Callable[[], datetime]
    authority: str = "shop.countersign.dev"
    signer: receipts.MerchantSigner | None = None
    store: MemoryReceiptStore = field(default_factory=MemoryReceiptStore)
    directory: tap.KeyDirectory | None = None
    nonces: tap.MemoryNonceCache = field(default_factory=tap.MemoryNonceCache)
    edge_log: list[dict[str, Any]] = field(default_factory=list)

    def __post_init__(self) -> None:
        if self.directory is None:
            self.directory = tap.StaticDirectory(
                {(self.agent_key.signature_agent, self.agent_key.key_id): self.agent_key.key.public_key()}
            )

    @property
    def currency(self) -> str:
        return self.merchant.currency

    def _edge(self, path: str, tag: str, headers: dict[str, str] | None = None,
              body: bytes | None = None) -> tap.AgentIdentity:
        signed = headers if headers is not None else self.agent_key.sign(self.authority, path, tag, body)
        result = tap.verify(signed, authority=self.authority, path=path, directory=self.directory,
                            nonces=self.nonces, require_tag=tag, body=body,
                            require_body=tag == tap.PAY and body is not None)
        self.edge_log.append({"path": path, "tag": tag, "ok": isinstance(result, tap.AgentIdentity),
                              "detail": result.key_id if isinstance(result, tap.AgentIdentity) else result.reason})
        if isinstance(result, tap.Rejection):
            raise Refused(result.reason)
        return result

    def search(self, query: str, limit: int) -> list[Product]:
        self._edge(f"/products?{urllib.parse.urlencode({'q': query, 'limit': limit})}", tap.BROWSE)
        return self.merchant.search(query, limit)

    def offer(self, cart: dict[str, int], shopper_currency: str) -> Terms:
        self._edge("/offers", tap.BROWSE)
        return self.merchant.offer(cart, shopper_currency, self.clock())

    def checkout(self, approval: Approval, idempotency_key: str) -> Result:
        # The same bytes the HTTP client would send, so the signature binds the approval.
        body = json.dumps({"approval": wire.approval_to_json(approval), "idempotency_key": idempotency_key}).encode()
        identity = self._edge("/checkout", tap.PAY, body=body)
        return countersign(self.merchant, approval, idempotency_key, self.clock(),
                           agent=identity, signer=self.signer, store=self.store)


class HttpShop:
    """C7 over HTTP. Every call is TAP-signed for the exact path it hits."""

    def __init__(self, base_url: str, agent_key: AgentKey, currency: str = "USD", timeout: float = 20):
        self.currency = currency
        parsed = urllib.parse.urlsplit(base_url)
        self.base_url = base_url.rstrip("/")
        self.authority = parsed.netloc
        self.base_path = parsed.path.rstrip("/")
        self.agent_key = agent_key
        self.timeout = timeout

    def _transport(self, method: str, url: str, data: bytes | None, headers: dict[str, str]) -> tuple[int, bytes]:
        """Send one request; (status, body). Subclasses can route it elsewhere."""
        req = urllib.request.Request(url, data=data, method=method, headers=headers)
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                return resp.status, resp.read()
        except urllib.error.HTTPError as err:
            return err.code, err.read()

    def _call(self, method: str, path: str, tag: str, body: dict | None = None) -> Any:
        data = json.dumps(body).encode() if body is not None else None
        # TAP signs the path the merchant actually receives, base path included.
        signed_path = self.base_path + path
        # A request with a body signs its Content-Digest, so the body cannot be swapped.
        headers = {"Content-Type": "application/json", **self.agent_key.sign(self.authority, signed_path, tag, data)}
        status, raw = self._transport(method, f"{self.base_url}{path}", data, headers)
        payload = json.loads(raw or b"null")
        if 200 <= status < 300:
            return payload
        if status == 401:
            raise Refused((payload or {}).get("refused", "refused"))
        raise MerchantError(status, payload)

    def search(self, query: str, limit: int) -> list[Product]:
        from decimal import Decimal

        path = f"/products?{urllib.parse.urlencode({'q': query, 'limit': limit})}"
        return [Product(p["sku"], p["title"], p["category"], Decimal(p["price"]), p["stock"], p["delivery"])
                for p in self._call("GET", path, tap.BROWSE)]

    def offer(self, cart: dict[str, int], shopper_currency: str) -> Terms:
        return wire.terms_from_json(self._call("POST", "/offers", tap.BROWSE,
                                               {"cart": cart, "shopper_currency": shopper_currency}))

    def checkout(self, approval: Approval, idempotency_key: str) -> Result:
        for attempt in range(3):
            try:
                out = self._call("POST", "/checkout", tap.PAY,
                                 {"approval": wire.approval_to_json(approval), "idempotency_key": idempotency_key})
                break
            except (urllib.error.URLError, TimeoutError):
                # A timeout is not proof the charge failed: ask before retrying.
                existing = self._existing(idempotency_key)
                if existing is not None:
                    return existing
                time.sleep(0.5 * 2**attempt)
        else:
            raise TimeoutError("checkout did not answer and no order exists under this key")
        order = out.get("order")
        return Result(wire.decision_from_json(out["decision"]),
                      wire.order_from_json(order) if order else None, out.get("receipt"))

    def _existing(self, idempotency_key: str) -> Result | None:
        try:
            order = self._call("GET", f"/orders/{urllib.parse.quote(idempotency_key)}", tap.PAY)
        except MerchantError as err:
            if err.status == 404:
                return None
            raise
        o = wire.order_from_json(order)
        return Result(Decision(Outcome.PROCEED, "already paid under this idempotency key",
                               charge=o.amount, currency=o.currency), o)


class InProcessShop(HttpShop):
    """HttpShop against a merchant edge app running in this process.

    Every call is a real HTTP request through the edge's whole stack (TAP
    middleware, body binding, routing, per-key locks), only without a socket.
    The demo app uses it so each visitor gets their own merchant and edge.
    """

    def __init__(self, edge_app: Any, agent_key: AgentKey, authority: str, currency: str = "USD"):
        from fastapi.testclient import TestClient

        super().__init__(f"http://{authority}", agent_key, currency)
        self.client = TestClient(edge_app, base_url=f"http://{authority}")

    def _transport(self, method: str, url: str, data: bytes | None, headers: dict[str, str]) -> tuple[int, bytes]:
        response = self.client.request(method, url, content=data, headers=headers)
        return response.status_code, response.content


def new_idempotency_key() -> str:
    return str(uuid.uuid4())
