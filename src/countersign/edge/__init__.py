"""The merchant's edge: the C7 HTTP API, with a TAP check in front of every route."""

from .app import create_app

__all__ = ["create_app"]
