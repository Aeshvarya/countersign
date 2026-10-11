"""V2: the merchant's edge, the C7 HTTP API behind a TAP check.

Every request is verified before any route runs. A failed check answers
401 {"refused": "<reason>"} and nothing else happens: no search, no offer, no
charge. Browsing needs an agent-browser-auth signature; paying and looking up
an order need agent-payer-auth.

The authority checked is the merchant's own host, passed in, never the Host
header the caller sends: a signature made for another merchant must not verify
here just because the caller says it is somewhere else.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Callable

from fastapi import FastAPI, Query, Request
from fastapi.responses import JSONResponse

from .. import tap, wire
from ..merchant import MerchantGateway

# The tag each route needs. Anything not listed still needs a valid signature.
ROUTE_TAGS = (
    ("GET", "/products", tap.BROWSE),
    ("POST", "/offers", tap.BROWSE),
    ("POST", "/checkout", tap.PAY),
    ("GET", "/orders/", tap.PAY),
)


def required_tag(method: str, path: str) -> str | None:
    for m, prefix, tag in ROUTE_TAGS:
        if method == m and (path == prefix or (prefix.endswith("/") and path.startswith(prefix))):
            return tag
    return None


def signed_path(request: Request) -> str:
    """The path as sent, query string included: what Visa's proxy signs (req.url)."""
    raw = request.scope.get("raw_path") or request.url.path.encode()
    query = request.scope.get("query_string") or b""
    return raw.decode("latin-1") + (f"?{query.decode('latin-1')}" if query else "")


def create_app(
    merchant: MerchantGateway,
    directory: tap.KeyDirectory,
    *,
    authority: str,
    nonces: tap.NonceCache | None = None,
    clock: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
) -> FastAPI:
    app = FastAPI(title="Countersign merchant edge", docs_url=None, redoc_url=None, openapi_url=None)
    nonce_cache = nonces if nonces is not None else tap.MemoryNonceCache()
    edge_log: list[dict[str, Any]] = []
    app.state.edge_log = edge_log

    @app.middleware("http")
    async def tap_check(request: Request, call_next):
        path = signed_path(request)
        tag = required_tag(request.method, request.url.path)
        result = tap.verify(dict(request.headers), authority=authority, path=path, directory=directory,
                            nonces=nonce_cache, require_tag=tag)
        ok = isinstance(result, tap.AgentIdentity)
        edge_log.append({"path": path, "tag": tag, "ok": ok, "detail": result.key_id if ok else result.reason})
        del edge_log[:-500]
        if not ok:
            return JSONResponse({"refused": result.reason}, status_code=401)
        request.state.agent = result
        return await call_next(request)

    @app.get("/products")
    def products(q: str = "", limit: int = Query(5, ge=1, le=50)) -> list[dict[str, Any]]:
        return [wire.product(p) for p in merchant.search(q, limit)]

    return app
