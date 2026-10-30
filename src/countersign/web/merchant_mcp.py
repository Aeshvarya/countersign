"""The fallback merchant MCP: ours, ACP-shaped, over the same /acp routes Airwallex calls.

Airwallex hosts a merchant MCP per merchant and calls our create_checkout (C10)
when an agent checks out. If that is not live on our account in time, this
door stands in for it, and only for it: the shopper's agent (Claude Code,
Claude Desktop) connects here with a bearer token, and every checkout tool is
an HMAC-signed call to /acp/checkout_sessions, exactly the call Airwallex would
make. So the countersign, the idempotency keys and the receipts are the ones
the real path runs; nothing behind /acp knows which platform called.

    claude mcp add --transport http countersign https://<host>/acp/mcp \\
        --header "Authorization: Bearer $COUNTERSIGN_MCP_TOKEN"

MCP over streamable HTTP, stateless: every JSON-RPC request is one POST
answered with one JSON body. No server-sent events, no sessions.

Configuration: COUNTERSIGN_MCP_TOKEN (at least 32 characters) mounts the door.
It needs /acp, so COUNTERSIGN_ACP_HMAC_SECRET must be set too.
"""

from __future__ import annotations

import hmac
import json
import re
import secrets
from decimal import Decimal, InvalidOperation
from typing import Any, Callable

import httpx
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, Response

from ..edge import acp as acp_api
from ..edge import hmac_auth
from ..edge.app import body_refusal

MCP_PATH = "/acp/mcp"
PROTOCOL_VERSIONS = ("2025-11-25", "2025-06-18", "2025-03-26")  # newest first
SERVER_INFO = {"name": "countersign-merchant", "title": "Countersign merchant (fallback MCP)", "version": "0.1.0"}
SESSION_ID = re.compile(r"^cs_[0-9a-f]{24}$")

INSTRUCTIONS = (
    "This is a merchant's checkout. Search with search_products, then create_checkout with the items and the "
    "shopper's currency. Show the shopper the total it returns and get a clear yes before complete_checkout. "
    "complete_checkout charges only if the live terms still match what the shopper was shown: if the price, "
    "rate, stock or delivery moved, it charges nothing and returns messages with resolution "
    "requires_buyer_review. Show the shopper the new total and ask again; never retry on their behalf."
)

_ITEMS = {"type": "array", "minItems": 1, "description": "The whole cart.",
          "items": {"type": "object", "required": ["id", "quantity"], "additionalProperties": False,
                    "properties": {"id": {"type": "string", "description": "Product id from search_products"},
                                   "quantity": {"type": "integer", "minimum": 1}}}}
_CURRENCY = {"type": "string", "pattern": "^[A-Za-z]{3}$", "description": "The shopper's currency, e.g. INR"}
_CEILING = {"type": "string", "pattern": r"^\d+(\.\d+)?$",
            "description": "Optional: the most the shopper will pay, in their currency, e.g. \"4500.00\". "
                           "Small rate moves under it still go through; anything over it asks again."}
_SESSION = {"type": "string", "description": "The id create_checkout returned (cs_...)"}
_KEY = {"type": "string", "pattern": "^[A-Za-z0-9._:-]{1,128}$",
        "description": "Optional: reuse it when retrying the same call after a timeout"}


def _tool(name: str, title: str, description: str, properties: dict[str, Any], required: list[str],
          read_only: bool = False) -> dict[str, Any]:
    return {"name": name, "title": title, "description": description,
            "inputSchema": {"type": "object", "properties": properties, "required": required,
                            "additionalProperties": False},
            "annotations": {"readOnlyHint": read_only, "openWorldHint": False}}


TOOLS = [
    _tool("search_products", "Search products", "Find products in this merchant's catalogue.",
          {"query": {"type": "string"}, "limit": {"type": "integer", "minimum": 1, "maximum": 20}},
          ["query"], read_only=True),
    _tool("create_checkout", "Create checkout",
          "Price a cart in the shopper's currency with a fresh rate lock. Charges nothing.",
          {"items": _ITEMS, "currency": _CURRENCY, "ceiling": _CEILING, "idempotency_key": _KEY},
          ["items", "currency"]),
    _tool("update_checkout", "Update checkout", "Change the cart, currency or ceiling and price it again. "
          "Charges nothing.", {"checkout_session_id": _SESSION, "items": _ITEMS, "currency": _CURRENCY,
                               "ceiling": _CEILING}, ["checkout_session_id"]),
    _tool("get_checkout", "Get checkout", "Read a checkout, its order and payment, and the merchant's signed "
          "receipt once the payment is confirmed.", {"checkout_session_id": _SESSION},
          ["checkout_session_id"], read_only=True),
    _tool("complete_checkout", "Complete checkout",
          "Pay for the terms the shopper approved. The merchant re-checks them live first and charges only "
          "if they still hold. Safe to repeat: one checkout is charged at most once.",
          {"checkout_session_id": _SESSION, "idempotency_key": _KEY}, ["checkout_session_id"]),
    _tool("cancel_checkout", "Cancel checkout", "Cancel a checkout that has not been paid.",
          {"checkout_session_id": _SESSION}, ["checkout_session_id"]),
]
_NAMES = {t["name"] for t in TOOLS}


class ToolError(Exception):
    """A tool ran and failed: reported to the model as isError, never as a protocol error."""


class AcpCaller:
    """Signs and sends one call to /acp, as Airwallex would. In process: no network hop."""

    def __init__(self, edge: Any, config: acp_api.AcpConfig) -> None:
        self.edge = edge
        self.config = config

    async def __call__(self, method: str, path: str, body: dict[str, Any] | None = None,
                       key: str | None = None) -> tuple[int, dict[str, Any]]:
        raw = json.dumps(body).encode() if body is not None else b""
        headers = {"api-version": acp_api.API_VERSION,
                   **hmac_auth.sign(raw, self.config.secret, scheme=self.config.scheme)}
        if body is not None:
            headers["content-type"] = "application/json"
        if method == "POST":
            headers["idempotency-key"] = key or f"mcp-{secrets.token_hex(12)}"
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=self.edge), base_url="http://house") as client:
            r = await client.request(method, path, content=raw, headers=headers)
        return r.status_code, r.json()


def _money(minor: int, currency: str) -> str:
    return f"{acp_api.from_minor(minor, currency)} {currency}"


def _session_id(args: dict[str, Any]) -> str:
    sid = args.get("checkout_session_id")
    if not isinstance(sid, str) or not SESSION_ID.match(sid):
        raise ToolError("checkout_session_id must be an id create_checkout returned (cs_...)")
    return sid


def _cart_fields(args: dict[str, Any], currency: str | None) -> dict[str, Any]:
    """ACP fields from tool arguments. The ceiling arrives as a decimal and leaves in minor units."""
    body: dict[str, Any] = {}
    if "items" in args:
        body["items"] = args["items"]
    if "currency" in args:
        body["currency"] = args["currency"]
    if "ceiling" in args:
        if currency is None:
            raise ToolError("give currency with ceiling, so the ceiling is read in the right currency")
        try:
            body["ceiling"] = acp_api.to_minor(Decimal(str(args["ceiling"])), currency.upper())
        except (InvalidOperation, ValueError) as err:
            raise ToolError(f"ceiling: {err}") from None
    return body


class MerchantMcp:
    def __init__(self, house: Any, call_acp: Callable, public_url: str | None) -> None:
        self.house = house
        self.acp = call_acp
        self.public_url = (public_url or "").rstrip("/")

    # ------------------------------------------------------------ the tools

    async def run(self, name: str, args: dict[str, Any]) -> dict[str, Any]:
        if name == "search_products":
            return self.search(args)
        if name == "create_checkout":
            status, view = await self.acp("POST", acp_api.PREFIX, _cart_fields(args, args.get("currency")),
                                          args.get("idempotency_key"))
        elif name == "update_checkout":
            sid = _session_id(args)
            currency = args.get("currency")
            if currency is None and "ceiling" in args:
                _, current = await self.acp("GET", f"{acp_api.PREFIX}/{sid}")
                currency = current.get("currency")
            status, view = await self.acp("POST", f"{acp_api.PREFIX}/{sid}", _cart_fields(args, currency))
        elif name == "get_checkout":
            status, view = await self.acp("GET", f"{acp_api.PREFIX}/{_session_id(args)}")
        elif name == "complete_checkout":
            status, view = await self.acp("POST", f"{acp_api.PREFIX}/{_session_id(args)}/complete", {},
                                          args.get("idempotency_key"))
        else:
            status, view = await self.acp("POST", f"{acp_api.PREFIX}/{_session_id(args)}/cancel", {})
        if status >= 400:
            raise ToolError(view.get("message") or view.get("refused") or f"the checkout answered {status}")
        return {"checkout": view, "payment": self.payment(view)}

    def search(self, args: dict[str, Any]) -> dict[str, Any]:
        query = args.get("query")
        limit = args.get("limit", 5)
        if not isinstance(query, str):
            raise ToolError("query must be text")
        if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 20:
            raise ToolError("limit must be a whole number from 1 to 20")
        merchant = self.house.merchant
        return {"products": [{
            "id": p.sku, "title": p.title, "category": p.category,
            "price": f"{p.price} {merchant.currency}",
            "availability": "in_stock" if p.stock > 0 else "out_of_stock",
            "delivery": p.delivery,
            "link": f"{self.public_url}/p/{p.sku}" if self.public_url else f"/p/{p.sku}",
        } for p in merchant.search(query, limit)]}

    def payment(self, view: dict[str, Any]) -> dict[str, Any] | None:
        """The order's real payment state, and the receipt once it is signed.

        The ACP view says "completed" as soon as an order exists; a card payment
        can still be pending in Drop-in then. The countersign's key for an ACP
        session is "acp:<session id>" (edge/acp.py, complete).
        """
        if not view.get("order"):
            return None
        key = f"acp:{view['id']}"
        order = self.house.merchant.find_order(key)
        if order is None:
            return None
        return {"status": order.status, "order_number": order.order_number,
                "amount": f"{order.amount} {order.currency}", "receipt": self.house.store.get(key)}

    # ------------------------------------------------------------ JSON-RPC

    async def dispatch(self, msg: dict[str, Any]) -> dict[str, Any]:
        method, params, rid = msg["method"], msg.get("params") or {}, msg["id"]
        if not isinstance(params, dict):
            return _error(rid, -32602, "params must be an object")
        if method == "initialize":
            asked = params.get("protocolVersion")
            return _result(rid, {"protocolVersion": asked if asked in PROTOCOL_VERSIONS else PROTOCOL_VERSIONS[0],
                                 "capabilities": {"tools": {"listChanged": False}},
                                 "serverInfo": SERVER_INFO, "instructions": INSTRUCTIONS})
        if method == "ping":
            return _result(rid, {})
        if method == "tools/list":
            return _result(rid, {"tools": TOOLS})
        if method == "tools/call":
            name, args = params.get("name"), params.get("arguments") or {}
            if name not in _NAMES:
                return _error(rid, -32602, f"unknown tool: {name}")
            if not isinstance(args, dict):
                return _error(rid, -32602, "arguments must be an object")
            try:
                out = await self.run(name, args)
            except ToolError as err:
                return _result(rid, {"content": [{"type": "text", "text": str(err)}], "isError": True})
            return _result(rid, {"content": [{"type": "text", "text": _summary(name, out)}],
                                 "structuredContent": out, "isError": False})
        return _error(rid, -32601, f"method not found: {method}")


def _result(rid: Any, result: dict[str, Any]) -> dict[str, Any]:
    return {"jsonrpc": "2.0", "id": rid, "result": result}


def _error(rid: Any, code: int, message: str) -> dict[str, Any]:
    return {"jsonrpc": "2.0", "id": rid, "error": {"code": code, "message": message}}


def _summary(name: str, out: dict[str, Any]) -> str:
    """One readable paragraph first (amounts in the shopper's units, not minor units), then the JSON."""
    if name == "search_products":
        lines = [f"{p['id']}: {p['title']}, {p['price']}, {p['availability']}" for p in out["products"]]
        head = "\n".join(lines) or "No products matched."
    else:
        view, pay = out["checkout"], out["payment"]
        cur = view["currency"].upper()
        parts = [f"Checkout {view['id']} is {view['status']}."]
        total = next((t["amount"] for t in view["totals"] if t["type"] == "total"), None)
        if total is not None:
            parts.append(f"Total {_money(total, cur)}.")
        for line in view["line_items"]:
            parts.append(f"{line['item']['quantity']} x {line['item']['id']}: {_money(line['total'], cur)}.")
        if pay:
            parts.append(f"Order {pay['order_number']}, payment {pay['status']}"
                         f"{', receipt signed' if pay['receipt'] else ', no receipt yet'}.")
        parts += [f"{m['type']} ({m.get('code')}): {m['content']}" for m in view["messages"]]
        head = " ".join(parts)
    return f"{head}\n\n{json.dumps(out, indent=1)}"


def mcp_app(house: Any, token: bytes, public_url: str | None) -> FastAPI:
    """POST /acp/mcp. Bearer token first, then the body cap, then JSON-RPC."""
    api = house.edge.state.acp
    tools = MerchantMcp(house, AcpCaller(house.edge, api.config), public_url)
    allowed_origin = public_url.rstrip("/") if public_url else None
    app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)

    @app.post(MCP_PATH)
    async def rpc(request: Request):
        auth = request.headers.get("authorization", "")
        given = auth[7:].strip().encode() if auth[:7].lower() == "bearer " else b""
        if not given or not hmac.compare_digest(given, token):
            return JSONResponse({"error": "a valid bearer token is required"}, status_code=401,
                                headers={"WWW-Authenticate": "Bearer"})
        origin = request.headers.get("origin")
        if origin is not None and origin.rstrip("/") != allowed_origin:  # DNS rebinding, per the MCP spec
            return JSONResponse({"error": "this origin may not call the MCP"}, status_code=403)
        refusal = body_refusal(request.headers.get("content-length"))
        if refusal is not None:
            return JSONResponse({"error": refusal[1]}, status_code=refusal[0])
        version = request.headers.get("mcp-protocol-version")
        if version is not None and version not in PROTOCOL_VERSIONS:
            return JSONResponse(_error(None, -32600, f"MCP-Protocol-Version {version} is not supported"),
                                status_code=400)
        try:
            msg = json.loads(await request.body())
        except ValueError:
            return JSONResponse(_error(None, -32700, "the body is not JSON"), status_code=400)
        if not isinstance(msg, dict) or msg.get("jsonrpc") != "2.0":
            return JSONResponse(_error(None, -32600, "send one JSON-RPC 2.0 message per request"), status_code=400)
        if "id" not in msg or "method" not in msg:  # a notification, or a reply to us: nothing to answer
            return Response(status_code=202)
        if not isinstance(msg["method"], str):
            return JSONResponse(_error(msg["id"], -32600, "method must be a string"), status_code=400)
        return JSONResponse(await tools.dispatch(msg))

    @app.api_route(MCP_PATH, methods=["GET", "DELETE"])
    async def no_stream():
        return Response(status_code=405, headers={"Allow": "POST"})

    return app
