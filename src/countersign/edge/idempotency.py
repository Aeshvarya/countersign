"""Idempotency keys for the checkout API, with the Agentic Commerce Protocol's rules.

- Same key, equivalent request: the stored response again, marked replayed.
- Same key, a different request: 422 idempotency_conflict. Not a retry.
- Same key while the first request is still running: 409 idempotency_in_flight,
  with Retry-After, so the caller waits instead of paying twice.
- A 5xx is never stored: the caller may retry it.

Kept in memory for at least 24 hours. A restart forgets keys; the charge itself
is still protected by the merchant's own idempotency key per session (C6).
"""

from __future__ import annotations

import hashlib
import json
import threading
import time
from dataclasses import dataclass
from typing import Any, Callable

RETENTION_SECONDS = 24 * 3600


def fingerprint(method: str, path: str, body: Any) -> str:
    """Equivalent requests share a fingerprint: same method, path and JSON value."""
    canonical = json.dumps(body, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(f"{method} {path}\n{canonical}".encode()).hexdigest()


@dataclass
class Stored:
    status: int
    body: Any
    headers: dict[str, str]


@dataclass
class _Entry:
    fingerprint: str
    created: float
    response: Stored | None = None  # None while the first request is running


class IdempotencyStore:
    def __init__(self, clock: Callable[[], float] = time.time) -> None:
        self._entries: dict[str, _Entry] = {}
        self._lock = threading.Lock()
        self._clock = clock

    def begin(self, key: str, fp: str) -> tuple[str, Stored | None]:
        """("new", None) to go ahead; ("replay", stored); ("conflict", None); ("in_flight", None)."""
        now = self._clock()
        with self._lock:
            self._entries = {k: e for k, e in self._entries.items() if now - e.created < RETENTION_SECONDS}
            entry = self._entries.get(key)
            if entry is None:
                self._entries[key] = _Entry(fp, now)
                return "new", None
            if entry.fingerprint != fp:
                return "conflict", None
            if entry.response is None:
                return "in_flight", None
            return "replay", entry.response

    def finish(self, key: str, response: Stored) -> None:
        with self._lock:
            entry = self._entries.get(key)
            if entry is None:
                return
            if response.status >= 500:
                del self._entries[key]  # never cache a server error
            else:
                entry.response = response

    def abandon(self, key: str) -> None:
        """The first request died before answering: let a retry run."""
        with self._lock:
            entry = self._entries.get(key)
            if entry is not None and entry.response is None:
                del self._entries[key]
