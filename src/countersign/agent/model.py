"""The model behind the agent: Claude, or a scripted stand-in for offline runs.

Both return objects with `.content` (blocks with `.type`, and `.text` or
`.id`/`.name`/`.input`) and `.stop_reason`, the shape of an Anthropic Message,
so the session loop cannot tell them apart.
"""

from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass, field
from typing import Any, Protocol


class Model(Protocol):
    def respond(self, system: str, tools: list[dict], messages: list[dict]) -> Any: ...


class ClaudeModel:
    """Claude through the Anthropic SDK. The model ID comes from the environment."""

    def __init__(self, model: str | None = None, effort: str = "medium"):
        import anthropic

        self.client = anthropic.Anthropic()
        self.model = model or os.environ.get("COUNTERSIGN_AGENT_MODEL") or "claude-opus-5"
        self.effort = effort

    def respond(self, system: str, tools: list[dict], messages: list[dict]) -> Any:
        # Server-side refusal fallback: if the model declines, the API reruns
        # the same request on a fallback model inside the same call.
        return self.client.beta.messages.create(
            model=self.model,
            max_tokens=16000,
            system=system,
            tools=tools,
            messages=messages,
            output_config={"effort": self.effort},
            betas=["server-side-fallback-2026-07-01"],
            fallbacks="default",
        )


# ------------------------------------------------------------- scripted model


@dataclass
class Block:
    type: str
    text: str = ""
    id: str = ""
    name: str = ""
    input: dict = field(default_factory=dict)


@dataclass
class Reply:
    content: list[Block]
    stop_reason: str


def _last_tool_exchange(messages: list[dict]) -> tuple[str | None, dict | None]:
    """The name of the last tool called and its parsed result, if any."""
    results = next((m["content"] for m in reversed(messages) if m["role"] == "user" and isinstance(m["content"], list)), None)
    calls = next((m["content"] for m in reversed(messages) if m["role"] == "assistant"), None)
    if not results or not calls:
        return None, None
    result = next((r for r in results if r.get("type") == "tool_result"), None)
    call = next((b for b in calls if getattr(b, "type", None) == "tool_use" and b.id == result["tool_use_id"]), None) if result else None
    if call is None:
        return None, None
    return call.name, json.loads(result["content"])


class ScriptedModel:
    """A deterministic shopper's agent: search, pick, offer, ask, pay.

    It follows the same tools and rules a real model gets, which makes the
    loop testable and the demo runnable without a key. It picks the cheapest
    in-stock result that matches the request's "under N" budget if one is given.
    """

    def __init__(self) -> None:
        self.calls = 0

    def _tool(self, name: str, args: dict, text: str = "") -> Reply:
        self.calls += 1
        blocks = [Block("text", text=text)] if text else []
        blocks.append(Block("tool_use", id=f"toolu_{self.calls:03d}", name=name, input=args))
        return Reply(blocks, "tool_use")

    def _say(self, text: str) -> Reply:
        return Reply([Block("text", text=text)], "end_turn")

    def respond(self, system: str, tools: list[dict], messages: list[dict]) -> Any:
        request = next(m["content"] for m in messages if m["role"] == "user")
        name, result = _last_tool_exchange(messages)
        if name is None:
            query = re.sub(r"\b(buy|me|a|an|the|please|under|below|for|\d[\d,]*|₹|inr|usd|\$)\b", " ", request.lower())
            return self._tool("search_products", {"query": " ".join(query.split()) or request, "limit": 5},
                              "Looking through the merchant's catalogue.")
        if result is not None and "error" in result and name != "get_offer":
            return self._say(f"I couldn't continue: {result['error']}. Nothing was charged.")
        if name == "search_products":
            if not result["products"]:
                return self._say("I couldn't find anything matching that.")
            budget = re.search(r"under\s*[₹$]?\s*([\d,]+)", request.lower())
            pick = sorted(result["products"], key=lambda p: float(p["price"]))[0]
            note = f"Cheapest match: {pick['title']}."
            if budget:
                note += f" (Budget {budget.group(1)} noted; I'll check the price in your currency.)"
            return self._tool("get_offer", {"items": [{"sku": pick["sku"], "quantity": 1}]}, note)
        if name == "get_offer":
            if "error" in result:
                return self._say(f"The merchant couldn't make an offer: {result['error']}")
            return self._tool("request_approval", {"offer_id": result["offer_id"],
                                                   "summary": f"{result['items'][0]['title']}, delivered {result['delivery']}."},
                              "Here are the exact terms for you to approve.")
        if name == "request_approval":
            if not result.get("approved"):
                return self._say("Understood, I won't buy it.")
            return self._tool("checkout", {"offer_id": result["offer_id"]}, "Approved. Asking the merchant to countersign.")
        if name == "checkout":
            outcome = result["outcome"]
            if outcome == "proceed":
                return self._say(f"Done. Order {result['order_number']}, charged {result['charged']}. The merchant's receipt is saved.")
            if outcome == "reask":
                changes = "; ".join(f"{c['field']}: {c['approved']} → {c['live']}" for c in result["changes"])
                sku_qty = result["cart"]
                return self._tool("get_offer", {"items": sku_qty}, f"Something changed since you approved ({changes}). Getting fresh terms.")
            return self._say(f"The merchant refused: {result['reason']}. Nothing was charged.")
        return self._say("Done.")
