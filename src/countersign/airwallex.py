"""A small Airwallex REST client: login, bearer token reuse, request ids, 429 backoff.

Standard library only. Conventions from the Airwallex builder guide:
- base URL https://api.sandbox.airwallex.com, paths under /api/v1
- POST /api/v1/authentication/login with x-client-id and x-api-key → a 30-minute token
- amounts are major units
- every write carries a fresh request_id; reuse one only to retry the same write
"""

from __future__ import annotations

import json
import os
import time
import http.client
import urllib.error
import urllib.parse
import urllib.request
import uuid
from dataclasses import dataclass


# No usable answer from Airwallex at all: no connection, a timeout, a cut-off body, a body that
# is not JSON. Callers that answer "Airwallex could not be reached" catch these beside AirwallexError.
UNREACHABLE = (OSError, http.client.HTTPException, json.JSONDecodeError)


class AirwallexError(Exception):
    def __init__(self, status: int, body: dict | str):
        self.status = status
        self.body = body
        code = body.get("code") if isinstance(body, dict) else None
        super().__init__(f"Airwallex HTTP {status}{f' {code}' if code else ''}: {body}")


@dataclass
class _Token:
    value: str
    expires: float


class Airwallex:
    def __init__(self, client_id: str, api_key: str, base_url: str = "https://api.sandbox.airwallex.com", timeout: float = 20):
        self.client_id = client_id
        self.api_key = api_key
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout
        self._token: _Token | None = None

    @classmethod
    def from_env(cls) -> "Airwallex":
        missing = [k for k in ("AIRWALLEX_CLIENT_ID", "AIRWALLEX_API_KEY") if not os.environ.get(k)]
        if missing:
            raise RuntimeError(f"set {', '.join(missing)} in .env")
        return cls(
            os.environ["AIRWALLEX_CLIENT_ID"],
            os.environ["AIRWALLEX_API_KEY"],
            os.environ.get("AIRWALLEX_BASE_URL", "https://api.sandbox.airwallex.com"),
        )

    @staticmethod
    def request_id() -> str:
        return str(uuid.uuid4())

    def _send(self, method: str, path: str, headers: dict[str, str], body: dict | None) -> dict:
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(f"{self.base_url}{path}", data=data, method=method)
        req.add_header("Content-Type", "application/json")
        for k, v in headers.items():
            req.add_header(k, v)
        for attempt in range(4):
            try:
                with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                    raw = resp.read()
                    return json.loads(raw) if raw else {}
            except urllib.error.HTTPError as err:  # an answer: before UNREACHABLE, since HTTPError is an OSError
                raw = err.read().decode(errors="replace")
                try:
                    parsed: dict | str = json.loads(raw)
                except json.JSONDecodeError:
                    parsed = raw
                if err.code == 429 and attempt < 3:
                    time.sleep(0.5 * 2**attempt)
                    continue
                raise AirwallexError(err.code, parsed) from None
            except UNREACHABLE:
                # A GET changes nothing, so one more try is free. A POST is not retried here: whether
                # it reached Airwallex is unknown, and its caller's request_id decides what a retry means.
                if method == "GET" and attempt == 0:
                    time.sleep(0.5)
                    continue
                raise
        raise AssertionError("unreachable")

    def token(self) -> str:
        if self._token and time.time() < self._token.expires - 60:
            return self._token.value
        body = self._send(
            "POST",
            "/api/v1/authentication/login",
            {"x-client-id": self.client_id, "x-api-key": self.api_key},
            None,
        )
        # The token lasts 30 minutes; refresh a minute early rather than parse expires_at.
        self._token = _Token(body["token"], time.time() + 29 * 60)
        return self._token.value

    def get(self, path: str, params: dict | None = None) -> dict:
        if params:
            path = f"{path}?{urllib.parse.urlencode(params)}"
        return self._send("GET", f"/api/v1{path}", {"Authorization": f"Bearer {self.token()}"}, None)

    def post(self, path: str, body: dict | None = None, extra_headers: dict[str, str] | None = None) -> dict:
        headers = {"Authorization": f"Bearer {self.token()}", **(extra_headers or {})}
        return self._send("POST", f"/api/v1{path}", headers, body or {})

