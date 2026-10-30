"""The merchant's edge: the C7 HTTP API, with a TAP check in front of every route."""

from typing import Any

__all__ = ["create_app"]


def __getattr__(name: str) -> Any:
    # Loaded on first use, so the core can import edge.passkey (stdlib and
    # cryptography only) to check a receipt without the server extra installed.
    if name == "create_app":
        from .app import create_app

        return create_app
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
