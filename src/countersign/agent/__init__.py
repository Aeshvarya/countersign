"""The shopping agent: Claude (or a scripted stand-in) driving a TAP-signed merchant."""

from .model import ClaudeModel, ScriptedModel
from .session import AgentSession
from .shop import AgentKey, HttpShop, LocalShop, Refused

__all__ = ["AgentKey", "AgentSession", "ClaudeModel", "HttpShop", "LocalShop", "Refused", "ScriptedModel"]
