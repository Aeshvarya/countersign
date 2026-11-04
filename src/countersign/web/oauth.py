"""C14 (W2): the merchant OAuth server Claude signs the shopper in through.

Airwallex's Kit 10 guide (11-03): Claude finds this server through Airwallex's
MCP metadata, signs the shopper in here, and calls Airwallex's MCP with the
access token; Airwallex checks the token's signature against our JWKS.

    GET  /.well-known/openid-configuration     discovery (also at /.well-known/oauth-authorization-server)
    GET  /.well-known/jwks.json                the ES256 public key the tokens are signed with
    POST /oauth/register                       dynamic client registration (RFC 7591), public clients only
    GET  /oauth/authorize                      the sign-in page: PKCE S256, then the shopper's passkey
    POST /oauth/authorize                      the passkey assertion from that page; answers where to go next
    POST /oauth/token                          authorization_code (with the PKCE verifier) or refresh_token

The shopper is a guest (no awx_customer_id): signing in proves a passkey, not
a person, as on the approval page (edge/passkey.py). The subject is a hash of
the passkey's public key, so the same device is the same shopper every time,
and the page keeps the passkey under the same browser key as the approval page,
so the passkey that signs in is the one that later approves.

Nothing is stored. A client id, a sign-in request, an authorization code and a
refresh token are each a sealed value (JSON plus an HMAC under a key derived
from the signing key, with its own kind and expiry), so a restart on the free
host signs nobody out. The one memory is the set of codes and sign-in requests
already used, so each works once while the process lives; after a restart a
code could be used again for the rest of its two minutes, and only by whoever
holds the PKCE verifier.

Configuration:

    COUNTERSIGN_PUBLIC_URL           the issuer, exactly, without a trailing slash (what onboarding registers)
    COUNTERSIGN_OAUTH_SIGNING_KEY    base64 of a 32-byte P-256 private value; `python -m countersign.web.oauth`
                                     makes one. Without it a key is made at start and tokens stop
                                     verifying after a restart.
    COUNTERSIGN_OAUTH_AUDIENCE       the access token's aud when the client names no resource
                                     (default: none)
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import hmac
import json
import logging
import secrets
import string
import sys
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Mapping
from urllib.parse import parse_qsl, unquote, urlencode, urlparse

from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives.asymmetric.utils import decode_dss_signature
from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse, Response

from ..edge import passkey

log = logging.getLogger("countersign.oauth")

DISCOVERY_PATH = "/.well-known/openid-configuration"
METADATA_PATH = "/.well-known/oauth-authorization-server"
JWKS_PATH = "/.well-known/jwks.json"
REGISTER_PATH = "/oauth/register"
AUTHORIZE_PATH = "/oauth/authorize"
TOKEN_PATH = "/oauth/token"
PATHS = (DISCOVERY_PATH, METADATA_PATH, JWKS_PATH, REGISTER_PATH, AUTHORIZE_PATH, TOKEN_PATH)

SCOPES = ("openid", "email")
ACCESS_TTL = 3600  # seconds
REQUEST_TTL = 600  # the sign-in page stays usable this long
CODE_TTL = 120
REFRESH_TTL = 30 * 24 * 3600
CHALLENGE_LABEL = "countersign-oauth-v1"
PAGE = Path(__file__).parent / "static" / "signin.html"
_VERIFIER = frozenset(string.ascii_letters + string.digits + "-._~")  # RFC 7636 unreserved characters
_LOCAL_HOSTS = ("localhost", "127.0.0.1", "[::1]", "::1")


class OAuthError(Exception):
    """An RFC 6749 error: `error` is the code, the message its description."""

    def __init__(self, error: str, description: str, status: int = 400) -> None:
        super().__init__(description)
        self.error, self.status = error, status

    def body(self) -> dict[str, str]:
        return {"error": self.error, "error_description": str(self)}


# ---------------------------------------------------------------- keys and sealed values

def new_signing_key() -> str:
    """A fresh COUNTERSIGN_OAUTH_SIGNING_KEY value."""
    key = ec.generate_private_key(ec.SECP256R1())
    return base64.b64encode(key.private_numbers().private_value.to_bytes(32, "big")).decode()


def signing_key_from(environ: Mapping[str, str]) -> ec.EllipticCurvePrivateKey:
    value = environ.get("COUNTERSIGN_OAUTH_SIGNING_KEY", "").strip()
    if not value:
        log.warning("no COUNTERSIGN_OAUTH_SIGNING_KEY: access tokens stop verifying after a restart")
        return ec.generate_private_key(ec.SECP256R1())
    try:
        raw = base64.b64decode(value, validate=True)
    except binascii.Error:
        raise ValueError("COUNTERSIGN_OAUTH_SIGNING_KEY is not base64") from None
    if len(raw) != 32:
        raise ValueError("COUNTERSIGN_OAUTH_SIGNING_KEY must decode to 32 bytes")
    try:
        return ec.derive_private_key(int.from_bytes(raw, "big"), ec.SECP256R1())
    except ValueError:
        raise ValueError("COUNTERSIGN_OAUTH_SIGNING_KEY is not a valid P-256 private value") from None


def _json_bytes(value: Any) -> bytes:
    return json.dumps(value, separators=(",", ":"), sort_keys=True).encode("utf-8")


@dataclass
class Signer:
    """ES256 JWTs under one P-256 key, published as one JWK."""

    key: ec.EllipticCurvePrivateKey

    def jwk(self) -> dict[str, str]:
        numbers = self.key.public_key().public_numbers()
        jwk = {"crv": "P-256", "kty": "EC",
               "x": passkey.b64url(numbers.x.to_bytes(32, "big")), "y": passkey.b64url(numbers.y.to_bytes(32, "big"))}
        # RFC 7638 thumbprint: the required members in lexicographic order, no spaces.
        thumbprint = passkey.b64url(hashlib.sha256(_json_bytes(jwk)).digest())
        return {**jwk, "kid": thumbprint, "use": "sig", "alg": "ES256"}

    def sign(self, claims: Mapping[str, Any], *, typ: str = "JWT") -> str:
        header = {"alg": "ES256", "typ": typ, "kid": self.jwk()["kid"]}
        signing_input = passkey.b64url(_json_bytes(header)) + "." + passkey.b64url(_json_bytes(dict(claims)))
        r, s = decode_dss_signature(self.key.sign(signing_input.encode("ascii"), ec.ECDSA(hashes.SHA256())))
        return signing_input + "." + passkey.b64url(r.to_bytes(32, "big") + s.to_bytes(32, "big"))

    def seal_key(self) -> bytes:
        private = self.key.private_numbers().private_value.to_bytes(32, "big")
        return hashlib.sha256(b"countersign-oauth-seal-v1" + private).digest()


@dataclass
class Sealer:
    """JSON values only this server can make: each has a kind, so a code never passes as a refresh token."""

    key: bytes

    def seal(self, kind: str, payload: Mapping[str, Any]) -> str:
        body = passkey.b64url(_json_bytes(dict(payload)))
        return body + "." + passkey.b64url(self._mac(kind, body))

    def open(self, kind: str, value: Any, now: float) -> dict[str, Any] | None:
        """The payload, or None when the value is not ours, not this kind, or expired."""
        if not isinstance(value, str) or value.count(".") != 1:
            return None
        body, mac = value.split(".")
        try:
            if not hmac.compare_digest(passkey.b64url_decode(mac), self._mac(kind, body)):
                return None
            payload = json.loads(passkey.b64url_decode(body))
        except ValueError:
            return None
        if not isinstance(payload, dict):
            return None
        exp = payload.get("exp")
        if exp is not None and (not isinstance(exp, (int, float)) or now >= exp):
            return None
        return payload

    def _mac(self, kind: str, body: str) -> bytes:
        return hmac.new(self.key, f"{kind}.{body}".encode("ascii"), hashlib.sha256).digest()


# ---------------------------------------------------------------- the server

def redirect_uri_problem(uri: Any) -> str | None:
    """Why a redirect URI is refused: https anywhere, plain http only back to this machine (mcp-remote)."""
    if not isinstance(uri, str) or not uri or len(uri) > 2000:
        return "a redirect URI must be a non-empty string"
    parsed = urlparse(uri)
    if parsed.fragment or "#" in uri:
        return "a redirect URI must not have a fragment"
    if parsed.scheme == "https" and parsed.hostname:
        return None
    if parsed.scheme == "http" and parsed.hostname in _LOCAL_HOSTS:
        return None
    return "a redirect URI must be https, or http to localhost"


def subject_of(public_key_spki_der: bytes) -> str:
    """The shopper's id: the same passkey is the same guest, and nothing about the person is in it."""
    return "shopper_" + passkey.b64url(hashlib.sha256(public_key_spki_der).digest())[:22]


@dataclass
class OAuthServer:
    issuer: str  # exactly COUNTERSIGN_PUBLIC_URL, no trailing slash
    signer: Signer
    audience: str | None = None
    clock: Callable[[], float] = time.time
    sealer: Sealer = field(init=False)
    _used: dict[str, float] = field(default_factory=dict)  # sign-in request and code ids -> when they expire
    _passkeys: dict[str, bytes] = field(default_factory=dict)  # credential id -> the key it first signed with
    _lock: threading.Lock = field(default_factory=threading.Lock)

    def __post_init__(self) -> None:
        self.sealer = Sealer(self.signer.seal_key())
        parsed = urlparse(self.issuer)
        self.origin = f"{parsed.scheme}://{parsed.netloc}"
        self.rp_id = parsed.hostname or ""

    # discovery

    def metadata(self) -> dict[str, Any]:
        return {
            "issuer": self.issuer,
            "authorization_endpoint": self.issuer + AUTHORIZE_PATH,
            "token_endpoint": self.issuer + TOKEN_PATH,
            "registration_endpoint": self.issuer + REGISTER_PATH,
            "jwks_uri": self.issuer + JWKS_PATH,
            "response_types_supported": ["code"],
            "response_modes_supported": ["query"],
            "grant_types_supported": ["authorization_code", "refresh_token"],
            "code_challenge_methods_supported": ["S256"],
            "token_endpoint_auth_methods_supported": ["none"],
            "scopes_supported": list(SCOPES),
            "subject_types_supported": ["public"],
            "id_token_signing_alg_values_supported": ["ES256"],
            "claims_supported": ["iss", "sub", "aud", "exp", "iat", "nonce"],
            "authorization_response_iss_parameter_supported": True,
        }

    def jwks(self) -> dict[str, Any]:
        return {"keys": [self.signer.jwk()]}

    # registration

    def register(self, body: Any) -> dict[str, Any]:
        if not isinstance(body, dict):
            raise OAuthError("invalid_client_metadata", "the registration must be a JSON object")
        uris = body.get("redirect_uris")
        if not isinstance(uris, list) or not 1 <= len(uris) <= 10:
            raise OAuthError("invalid_redirect_uri", "redirect_uris must list one to ten URIs")
        for uri in uris:
            problem = redirect_uri_problem(uri)
            if problem:
                raise OAuthError("invalid_redirect_uri", problem)
        name = body.get("client_name")
        name = name.strip()[:80] if isinstance(name, str) and name.strip() else "An app"
        issued = int(self.clock())
        # The client id carries its own redirect URIs, so registering stores nothing and survives a restart.
        client_id = self.sealer.seal("client", {"redirect_uris": uris, "client_name": name, "iat": issued})
        return {"client_id": client_id, "client_id_issued_at": issued, "client_name": name, "redirect_uris": uris,
                "grant_types": ["authorization_code", "refresh_token"], "response_types": ["code"],
                "token_endpoint_auth_method": "none"}

    def client(self, client_id: Any) -> dict[str, Any] | None:
        return self.sealer.open("client", client_id, self.clock())

    # authorize

    def start(self, params: Mapping[str, str]) -> dict[str, Any]:
        """Check an authorization request and seal it for the sign-in page.

        Raises OAuthError with status 400 and no redirect while the client or its
        redirect URI is unproven; after that, an error carries `redirect` so the
        browser goes back to the client with it (RFC 6749 4.1.2.1).
        """
        client = self.client(params.get("client_id"))
        if client is None:
            raise OAuthError("invalid_client", "this app is not registered with the shop (or its registration is not ours)")
        redirect_uri = params.get("redirect_uri") or ""
        if not redirect_uri and len(client["redirect_uris"]) == 1:
            redirect_uri = client["redirect_uris"][0]
        if redirect_uri not in client["redirect_uris"]:
            raise OAuthError("invalid_request", "redirect_uri is not one this app registered")
        back = _Back(redirect_uri, params.get("state"), self.issuer)
        if params.get("response_type") != "code":
            raise back.error("unsupported_response_type", "only response_type=code is supported")
        challenge = params.get("code_challenge") or ""
        if not challenge:
            raise back.error("invalid_request", "code_challenge is required (PKCE S256)")
        if params.get("code_challenge_method") != "S256":
            raise back.error("invalid_request", "code_challenge_method must be S256")
        if not 43 <= len(challenge) <= 128 or not set(challenge) <= _VERIFIER:
            raise back.error("invalid_request", "code_challenge is not a valid S256 challenge")
        scope = " ".join(s for s in (params.get("scope") or "openid").split() if s in SCOPES) or "openid"
        resource = params.get("resource") or None
        if resource is not None and redirect_uri_problem(resource) is not None:
            raise back.error("invalid_target", "resource must be an absolute https URL")
        now = self.clock()
        request = {"client_id": params["client_id"], "redirect_uri": redirect_uri, "state": params.get("state"),
                   "code_challenge": challenge, "scope": scope, "resource": resource, "nonce": params.get("nonce"),
                   "rid": secrets.token_urlsafe(12), "exp": int(now) + REQUEST_TTL}
        sealed = self.sealer.seal("request", request)
        return {"request": sealed, "client_name": client["client_name"], "redirect_uri": redirect_uri,
                "state": params.get("state"), "issuer": self.issuer,
                "webauthn": {"challenge": passkey.b64url(self.challenge(sealed)), "rp_id": self.rp_id,
                             "origin": self.origin, "user_verification": "preferred"}}

    @staticmethod
    def challenge(sealed_request: str) -> bytes:
        """What the passkey signs: this one sign-in request, its client, redirect, PKCE challenge and expiry."""
        return hashlib.sha256(f"{CHALLENGE_LABEL}\n{sealed_request}".encode("ascii")).digest()

    def sign_in(self, body: Any) -> dict[str, str]:
        """The passkey assertion for a sealed request: where the browser goes next, with the code."""
        if not isinstance(body, dict):
            raise OAuthError("invalid_request", "the body must be a JSON object")
        now = self.clock()
        request = self.sealer.open("request", body.get("request"), now)
        if request is None:
            raise OAuthError("invalid_request", "this sign-in page has expired; start again from the app")
        raw: dict[str, bytes] = {}
        for name in ("credential_id", "public_key_spki_der", "authenticator_data", "client_data_json", "signature"):
            try:
                raw[name] = passkey.b64url_decode(body.get(name))
            except ValueError:
                raise OAuthError("invalid_request", f"{name} must be base64url") from None
        credential = passkey.b64url(raw["credential_id"])
        result = passkey.verify_assertion(
            public_key_spki_der=raw["public_key_spki_der"], authenticator_data=raw["authenticator_data"],
            client_data_json=raw["client_data_json"], signature=raw["signature"],
            expected_challenge=self.challenge(body["request"]), origin=self.origin, rp_id=self.rp_id)
        if isinstance(result, str):
            raise OAuthError("access_denied", f"the passkey was not accepted: {result}")
        with self._lock:
            known = self._passkeys.get(credential)
            if known is not None and known != raw["public_key_spki_der"]:
                raise OAuthError("access_denied", "this passkey is registered with a different key")
            self._spend(request["rid"], request["exp"], now, "invalid_request", "this sign-in page was already used")
            self._passkeys.setdefault(credential, raw["public_key_spki_der"])
        code = self.sealer.seal("code", {
            "client_id": request["client_id"], "redirect_uri": request["redirect_uri"],
            "code_challenge": request["code_challenge"], "scope": request["scope"],
            "resource": request["resource"], "nonce": request["nonce"],
            "sub": subject_of(raw["public_key_spki_der"]), "jti": secrets.token_urlsafe(12),
            "exp": int(now) + CODE_TTL})
        back = _Back(request["redirect_uri"], request["state"], self.issuer)
        return {"redirect": back.url({"code": code})}

    # token

    def token(self, form: Mapping[str, str], basic_client_id: str | None = None) -> dict[str, Any]:
        client_id = form.get("client_id") or basic_client_id
        if self.client(client_id) is None:
            raise OAuthError("invalid_client", "unknown client_id", 401)
        grant = form.get("grant_type")
        now = self.clock()
        if grant == "authorization_code":
            code = self.sealer.open("code", form.get("code"), now)
            if code is None or code["client_id"] != client_id:
                raise OAuthError("invalid_grant", "the code is not valid, has expired, or is for another app")
            if form.get("redirect_uri") not in (None, "", code["redirect_uri"]):
                raise OAuthError("invalid_grant", "redirect_uri does not match the one the code was issued for")
            verifier = form.get("code_verifier") or ""
            if not 43 <= len(verifier) <= 128 or not set(verifier) <= _VERIFIER:
                raise OAuthError("invalid_grant", "code_verifier is missing or malformed")
            expected = passkey.b64url(hashlib.sha256(verifier.encode("ascii")).digest())
            if not hmac.compare_digest(expected, code["code_challenge"]):
                raise OAuthError("invalid_grant", "code_verifier does not match the code_challenge")
            with self._lock:
                self._spend(code["jti"], code["exp"], now, "invalid_grant", "this code was already used")
            return self._tokens(client_id, code["sub"], code["scope"], code["resource"], code["nonce"], now)
        if grant == "refresh_token":
            refresh = self.sealer.open("refresh", form.get("refresh_token"), now)
            if refresh is None or refresh["client_id"] != client_id:
                raise OAuthError("invalid_grant", "the refresh token is not valid, has expired, or is for another app")
            return self._tokens(client_id, refresh["sub"], refresh["scope"], refresh["resource"], None, now)
        raise OAuthError("unsupported_grant_type", "grant_type must be authorization_code or refresh_token")

    def _tokens(self, client_id: str, sub: str, scope: str, resource: str | None, nonce: str | None,
                now: float) -> dict[str, Any]:
        iat = int(now)
        claims = {"iss": self.issuer, "sub": sub, "client_id": client_id, "scope": scope,
                  "iat": iat, "exp": iat + ACCESS_TTL, "jti": secrets.token_urlsafe(12)}
        audience = resource or self.audience
        if audience:
            claims["aud"] = audience
        out = {"access_token": self.signer.sign(claims, typ="at+jwt"), "token_type": "Bearer",
               "expires_in": ACCESS_TTL, "scope": scope,
               "refresh_token": self.sealer.seal("refresh", {"client_id": client_id, "sub": sub, "scope": scope,
                                                             "resource": resource, "exp": iat + REFRESH_TTL})}
        if "openid" in scope.split():
            id_claims = {"iss": self.issuer, "sub": sub, "aud": client_id, "iat": iat, "exp": iat + ACCESS_TTL}
            if nonce:
                id_claims["nonce"] = nonce
            out["id_token"] = self.signer.sign(id_claims)
        return out

    def _spend(self, ident: str, exp: float, now: float, error: str, reused: str) -> None:
        """Mark a one-time value used (the caller holds the lock). Expired entries are dropped on the way."""
        for stale in [k for k, until in self._used.items() if until <= now]:
            del self._used[stale]
        if ident in self._used:
            raise OAuthError(error, reused)
        self._used[ident] = exp


@dataclass
class _Back:
    """The way back to the client: its redirect URI with our answer, the state and the issuer (RFC 9207)."""

    redirect_uri: str
    state: str | None
    issuer: str

    def url(self, params: Mapping[str, str]) -> str:
        query = dict(params)
        if self.state is not None:
            query["state"] = self.state
        query["iss"] = self.issuer
        return self.redirect_uri + ("&" if "?" in self.redirect_uri else "?") + urlencode(query)

    def error(self, error: str, description: str) -> OAuthError:
        err = OAuthError(error, description)
        err.redirect = self.url({"error": error, "error_description": description})  # type: ignore[attr-defined]
        return err


def server_from_env(environ: Mapping[str, str], *, clock: Callable[[], float] | None = None) -> OAuthServer | None:
    """The OAuth server, when there is a public URL to be the issuer; None otherwise."""
    public_url = (environ.get("COUNTERSIGN_PUBLIC_URL") or "").strip()
    if not public_url:
        return None
    extra = {"clock": clock} if clock is not None else {}
    return OAuthServer(public_url.rstrip("/"), Signer(signing_key_from(environ)),
                       (environ.get("COUNTERSIGN_OAUTH_AUDIENCE") or "").strip() or None, **extra)


# ---------------------------------------------------------------- HTTP

CORS = {"Access-Control-Allow-Origin": "*", "Access-Control-Allow-Methods": "GET, POST, OPTIONS",
        "Access-Control-Allow-Headers": "Authorization, Content-Type, MCP-Protocol-Version"}
NO_STORE = {"Cache-Control": "no-store", "Pragma": "no-cache"}


def _error_page(message: str, status: int = 400) -> HTMLResponse:
    from html import escape
    return HTMLResponse("<!doctype html><meta charset=utf-8><meta name=viewport content='width=device-width'>"
                        "<title>Sign-in problem</title><body style='font-family:system-ui;max-width:32rem;margin:3rem auto;"
                        f"padding:0 1rem'><h1>This sign-in cannot continue</h1><p>{escape(message)}</p>"
                        "<p>Go back to the app and start the sign-in again.</p>", status_code=status, headers=NO_STORE)


def _basic_client_id(request: Request) -> str | None:
    header = request.headers.get("authorization", "")
    if not header.lower().startswith("basic "):
        return None
    try:
        user = base64.b64decode(header[6:].strip(), validate=True).decode("utf-8").split(":", 1)[0]
    except (binascii.Error, UnicodeDecodeError):
        return None
    return unquote(user) or None


def oauth_app(server: OAuthServer) -> FastAPI:
    app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)
    page = PAGE.read_text(encoding="utf-8")

    @app.options("/{path:path}")
    def preflight(path: str) -> Response:
        return Response(status_code=204, headers=CORS)

    @app.get(DISCOVERY_PATH)
    @app.get(METADATA_PATH)
    def discovery() -> JSONResponse:
        return JSONResponse(server.metadata(), headers={**CORS, "Cache-Control": "public, max-age=300"})

    @app.get(JWKS_PATH)
    def jwks() -> JSONResponse:
        # Airwallex fetches this on a token check with a short timeout: it is built once and cacheable.
        return JSONResponse(server.jwks(), headers={**CORS, "Cache-Control": "public, max-age=3600"})

    @app.post(REGISTER_PATH)
    async def register(request: Request) -> JSONResponse:
        try:
            body = await request.json()
        except ValueError:
            body = None
        try:
            return JSONResponse(server.register(body), status_code=201, headers={**CORS, **NO_STORE})
        except OAuthError as err:
            return JSONResponse(err.body(), status_code=err.status, headers=CORS)

    @app.get(AUTHORIZE_PATH)
    def authorize(request: Request) -> Response:
        try:
            started = server.start(dict(request.query_params))
        except OAuthError as err:
            redirect = getattr(err, "redirect", None)
            if redirect:
                return Response(status_code=302, headers={"Location": redirect, **NO_STORE})
            return _error_page(str(err))
        data = json.dumps(started).replace("<", "\\u003c").replace(">", "\\u003e").replace("&", "\\u0026")
        return HTMLResponse(page.replace("{{SIGNIN_JSON}}", data),
                            headers={**NO_STORE, "Referrer-Policy": "no-referrer", "X-Frame-Options": "DENY",
                                     "Content-Security-Policy": "frame-ancestors 'none'"})

    @app.post(AUTHORIZE_PATH)
    async def sign_in(request: Request) -> JSONResponse:
        try:
            body = await request.json()
        except ValueError:
            body = None
        try:
            return JSONResponse(server.sign_in(body), headers=NO_STORE)
        except OAuthError as err:
            return JSONResponse(err.body(), status_code=err.status, headers=NO_STORE)

    @app.post(TOKEN_PATH)
    async def token(request: Request) -> JSONResponse:
        # application/x-www-form-urlencoded, read here so the host needs no multipart parser
        try:
            form = dict(parse_qsl((await request.body()).decode("utf-8"), keep_blank_values=True))
        except UnicodeDecodeError:
            form = {}
        try:
            return JSONResponse(server.token(form, _basic_client_id(request)), headers={**CORS, **NO_STORE})
        except OAuthError as err:
            headers = {**CORS, **NO_STORE}
            if err.status == 401:
                headers["WWW-Authenticate"] = 'Basic realm="token"'
            return JSONResponse(err.body(), status_code=err.status, headers=headers)

    return app


if __name__ == "__main__":
    if sys.argv[1:] not in ([], ["--new-key"]):
        sys.exit("usage: python -m countersign.web.oauth [--new-key]   prints a new COUNTERSIGN_OAUTH_SIGNING_KEY")
    print(new_signing_key())
