"""V10 live: onboard the merchant with Airwallex once, and check everything first (C10).

    set -a; . ./.env; set +a
    PYTHONPATH=src python scripts/ac_onboard.py                      # probe only
    PYTHONPATH=src python scripts/ac_onboard.py --check-host         # is /acp served?
    PYTHONPATH=src python scripts/ac_onboard.py --onboard --dry-run  # the body, secrets hidden
    PYTHONPATH=src python scripts/ac_onboard.py --onboard            # POST /onboard, once
    PYTHONPATH=src python scripts/ac_onboard.py --update --dry-run   # the config/update body, secret hidden
    PYTHONPATH=src python scripts/ac_onboard.py --update             # POST /config/update: apis + image domains

Probe, read-only: log in, then GET both config paths. The documented one is
/api/v1/pa/agentic_commerce/config; another Kit 10 team saw 401 "Admin
permissions required" on /api/v1/agentic-commerce/config with a scoped key
(#commerce-kits-9-and-10, 2026-10-28), so a 404 on ours alone does not prove the
feature is missing. Verdict: documented 200 = onboarded; documented 404
MERCHANT_CONFIG_NOT_FOUND = ready to onboard; a 401 on either = the key's scope,
make a full-access key; both 404 with no Airwallex code = not on the account.
--onboard only ever uses the documented path.

Host check: an unsigned POST to create_checkout must be refused by the ACP door
(401, code "unauthorized"). A TAP refusal ({"refused": ...}) means the host runs
the edge without COUNTERSIGN_ACP_HMAC_SECRET, so the /acp routes are not
mounted; a 404 means the host does not serve the edge at all. Either way
Airwallex's first call would fail, so --onboard sends nothing. Then the OAuth
server: no discovery document yet is a warning (onboarding stores the URLs
either way); one that disagrees with what onboarding registers (issuer, jwks_uri,
an endpoint missing, no PKCE S256) or a JWKS with no keys fails the check,
because every Claude sign-in would then fail.

The body follows Airwallex's guide of 2026-11-03: the PSP key pair; apis with
create_checkout ({base}/acp/create_checkout), the HMAC secret and the merchant
OAuth server (apis.oauth.acp: the issuer is the host itself, its JWKS at
/.well-known/jwks.json, scopes openid and email); and whitelisted_image_domains,
the host's scheme and name with no path, where the catalogue's images live.

Onboarding is one-shot: Airwallex ignores the payload of a second POST /onboard,
and changes go through POST /config/update, which replaces a whole section (the
PSP key pair, or create_checkout, secret and OAuth together). --update sends the
apis section whole, with the image domains, and never the PSP keys (an omitted
section stays as it is). The secret it sends replaces the stored one, so it must
be the one the host runs with.

Exits 0 when the thing asked for is true or done, 1 when something is not ready
yet, 2 when Airwallex refused the call. Prints status codes, Airwallex's code
and message, and non-secret config fields only: never a key, the HMAC secret, a
token, a request header or the signed product-upload URL.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
import urllib.error
import urllib.request
from typing import Callable, Mapping, TextIO
from urllib.parse import urlsplit

from countersign import catalog_gmc
from countersign.airwallex import Airwallex, AirwallexError
from countersign.edge.acp import CREATE_CHECKOUT, AcpConfig

CONFIG_PATH = "/pa/agentic_commerce/config"
OTHER_CONFIG_PATH = "/agentic-commerce/config"  # seen by another Kit 10 team; probed, never written to
ONBOARD_PATH = "/pa/agentic_commerce/onboard"
UPDATE_PATH = "/pa/agentic_commerce/config/update"
NOT_ONBOARDED = "MERCHANT_CONFIG_NOT_FOUND"
JWKS_PATH = "/.well-known/jwks.json"
DISCOVERY_PATH = "/.well-known/openid-configuration"
OIDC_ENDPOINTS = ("authorization_endpoint", "token_endpoint", "registration_endpoint")
OAUTH_SCOPES = ["openid", "email"]  # advertised to Claude; ACP does not enforce them
SLOW_SECONDS = 10.0  # a free Render instance waking from sleep takes longer than this

# fetch(method, url, body) -> (status, parsed JSON body or None, seconds taken)
Fetch = Callable[[str, str, bytes | None], tuple[int, object, float]]


def _code(err: AirwallexError) -> str:
    body = err.body if isinstance(err.body, dict) else {}
    return str(body.get("code") or "")


def _refusal(err: AirwallexError) -> str:
    body = err.body if isinstance(err.body, dict) else {}
    return " ".join([f"HTTP {err.status}"] + [str(body[k]) for k in ("code", "message") if body.get(k)])


def _get(client: Airwallex, path: str) -> tuple[dict | None, AirwallexError | None]:
    try:
        return client.get(path), None
    except AirwallexError as err:
        return None, err


def probe(client: Airwallex) -> tuple[str, dict | None, list[str]]:
    """The verdict, the config if onboarded, and one line per call (status, code, message; never a key).

    Verdicts: onboarded, not_onboarded (ready), key_scope, not_live, login_refused, unclear.
    """
    try:
        client.token()
    except AirwallexError as err:
        return "login_refused", None, [f"POST /api/v1/authentication/login -> {_refusal(err)}"]
    found = {path: _get(client, path) for path in (CONFIG_PATH, OTHER_CONFIG_PATH)}
    lines = [f"GET /api/v1{path} -> " + ("HTTP 200" if err is None else _refusal(err)) for path, (_, err) in found.items()]
    config, err = found[CONFIG_PATH]
    other = found[OTHER_CONFIG_PATH][1]
    if err is None:
        return "onboarded", config, lines
    if err.status == 404 and _code(err).upper() == NOT_ONBOARDED:
        return "not_onboarded", None, lines
    if 401 in (err.status, other.status if other else None):
        return "key_scope", None, lines
    if err.status == 404 and not _code(err) and other is not None and other.status == 404 and not _code(other):
        return "not_live", None, lines
    return "unclear", None, lines


def summary(config: Mapping) -> list[str]:
    """The non-secret fields of a config or onboard response, by allowlist."""
    psp = config.get("psp_config") or {}
    apis = config.get("apis") or {}
    upload = config.get("product_upload") or {}
    lines = [
        f"merchant_id: {config.get('merchant_id')}",
        f"status: {config.get('status')}",
        f"psp: {psp.get('psp_name')}, api key configured: {psp.get('api_key_configured')}, "
        f"managed payment: {psp.get('managed_payment')}",
        f"create_checkout: {apis.get('create_checkout')}",
        f"secret key configured: {apis.get('secret_key_configured')}",
        f"image domains: {', '.join(config.get('whitelisted_image_domains') or []) or 'none'}",
    ]
    for name, path in sorted((config.get("mcp_endpoints") or {}).items()):
        lines.append(f"mcp {name}: {path}")
    if upload.get("expires_at"):
        lines.append(f"product upload slot expires: {upload['expires_at']}")  # the URL itself is a credential
    return lines


def psp_keys(env: Mapping[str, str]) -> tuple[str, str, str]:
    """The PSP key pair Airwallex charges with, and a label for which one it is."""
    if env.get("AIRWALLEX_PSP_CLIENT_ID") or env.get("AIRWALLEX_PSP_API_KEY"):
        if not (env.get("AIRWALLEX_PSP_CLIENT_ID") and env.get("AIRWALLEX_PSP_API_KEY")):
            raise ValueError("set both AIRWALLEX_PSP_CLIENT_ID and AIRWALLEX_PSP_API_KEY, or neither")
        return env["AIRWALLEX_PSP_CLIENT_ID"], env["AIRWALLEX_PSP_API_KEY"], "a separate PSP key"
    return env.get("AIRWALLEX_CLIENT_ID", ""), env.get("AIRWALLEX_API_KEY", ""), "the login key"


def apis_section(env: Mapping[str, str], base_url: str) -> dict:
    """The whole apis section: Airwallex replaces it as one, so it always carries all three parts."""
    acp = AcpConfig.from_env(env)  # the same 32-byte rule the edge applies at start-up
    if acp is None:
        raise ValueError("COUNTERSIGN_ACP_HMAC_SECRET is not set")
    return {
        "create_checkout": create_checkout_url(base_url),
        "secret_key": acp.secret.decode(),
        "oauth": {"acp": {"oauth_base": oauth_base(base_url), "oauth_jwks": oauth_base(base_url) + JWKS_PATH,
                          "scopes": list(OAUTH_SCOPES)}},
    }


def onboard_body(env: Mapping[str, str], base_url: str) -> tuple[dict, str]:
    """The first POST /onboard body: both sections and the image domains. ValueError if a value is missing."""
    apis = apis_section(env, base_url)
    client_id, api_key, which = psp_keys(env)
    if not (client_id and api_key):
        raise ValueError("no PSP key: set AIRWALLEX_CLIENT_ID and AIRWALLEX_API_KEY")
    body = {
        "psp_config": {"api_key_data": {"client_id": client_id, "api_key": api_key}},
        "apis": apis,
        "whitelisted_image_domains": [image_domain(base_url)],
    }
    return body, which


def update_body(env: Mapping[str, str], base_url: str) -> dict:
    """POST /config/update: the apis section whole and the image domains; the PSP section is left out, so kept."""
    return {"apis": apis_section(env, base_url), "whitelisted_image_domains": [image_domain(base_url)]}


def redacted(body: dict) -> dict:
    hidden = json.loads(json.dumps(body))
    if "psp_config" in hidden:
        data = hidden["psp_config"]["api_key_data"]
        data["api_key"] = f"<set, {len(data['api_key'])} chars>"
    hidden["apis"]["secret_key"] = f"<set, {len(hidden['apis']['secret_key'])} chars>"
    return hidden


def create_checkout_url(base_url: str) -> str:
    return base_url.rstrip("/") + CREATE_CHECKOUT


def oauth_base(base_url: str) -> str:
    """The OAuth issuer: our own host (C14). Its discovery document's issuer must equal this exactly."""
    return base_url.rstrip("/")


def image_domain(base_url: str) -> str:
    """Scheme plus the full host the catalogue's /img links use, no path (Airwallex's rule)."""
    parts = urlsplit(base_url)
    return f"{parts.scheme}://{parts.netloc}"


def _urllib_fetch(method: str, url: str, body: bytes | None) -> tuple[int, object, float]:
    req = urllib.request.Request(url, data=body, method=method, headers={"content-type": "application/json"})
    start = time.monotonic()
    try:
        with urllib.request.urlopen(req, timeout=60) as resp:
            status, raw = resp.status, resp.read()
    except urllib.error.HTTPError as err:
        status, raw = err.code, err.read()
    seconds = time.monotonic() - start
    try:
        return status, json.loads(raw), seconds
    except ValueError:
        return status, None, seconds


def check_host(base_url: str, fetch: Fetch, *, sku: str | None = None) -> tuple[bool, list[str]]:
    """True if the host serves the ACP door and the catalogue pages, with one line per check."""
    lines, ok = [], True
    status, body, seconds = fetch("POST", create_checkout_url(base_url), b"{}")
    timing = f"{seconds:.1f} s" + (", slow: a cold start may time out Airwallex's call" if seconds > SLOW_SECONDS else "")
    if status == 401 and isinstance(body, dict) and body.get("code") == "unauthorized":
        lines.append(f"POST {CREATE_CHECKOUT} unsigned -> 401 from the ACP door ({timing})")
    elif isinstance(body, dict) and "refused" in body:
        ok = False
        lines.append(f"POST {CREATE_CHECKOUT} unsigned -> {status} from the TAP door: the host has no "
                     f"COUNTERSIGN_ACP_HMAC_SECRET, so /acp is not mounted ({timing})")
    else:
        ok = False
        lines.append(f"POST {CREATE_CHECKOUT} unsigned -> {status}: the host does not serve the edge's /acp routes ({timing})")

    sku = sku or catalog_gmc.catalog(base_url)[0]["id"]
    status, _, seconds = fetch("GET", f"{base_url.rstrip('/')}/p/{sku}", None)
    if status == 200:
        lines.append(f"GET /p/{sku} -> 200 ({seconds:.1f} s)")
    else:
        ok = False
        lines.append(f"GET /p/{sku} -> {status}: catalogue links would not resolve")

    oauth_ok, oauth_lines = check_oauth(base_url, fetch)
    return ok and oauth_ok, lines + oauth_lines


def oauth_problems(doc: Mapping, base_url: str) -> list[str]:
    """How a discovery document disagrees with what onboarding registers and Claude needs (guide, 11-03)."""
    issuer, jwks = oauth_base(base_url), oauth_base(base_url) + JWKS_PATH
    problems = []
    if doc.get("issuer") != issuer:
        problems.append(f"issuer is {doc.get('issuer')!r}, onboarding registers {issuer!r}")
    if doc.get("jwks_uri") != jwks:
        problems.append(f"jwks_uri is {doc.get('jwks_uri')!r}, onboarding registers {jwks!r}")
    missing = [name for name in OIDC_ENDPOINTS if not doc.get(name)]
    if missing:
        problems.append("no " + ", ".join(missing))
    if "S256" not in (doc.get("code_challenge_methods_supported") or []):
        problems.append("code_challenge_methods_supported lacks S256 (Claude uses PKCE)")
    return problems


def check_oauth(base_url: str, fetch: Fetch) -> tuple[bool, list[str]]:
    """The merchant OAuth server (C14) as Claude will find it. Not served yet is a warning, not a failure."""
    status, doc, _ = fetch("GET", oauth_base(base_url) + DISCOVERY_PATH, None)
    if status != 200 or not isinstance(doc, dict):
        return True, [f"GET {DISCOVERY_PATH} -> {status}: warning, the OAuth server is not served yet; "
                      "onboarding can go ahead, but Claude cannot sign a shopper in until it is"]
    problems = oauth_problems(doc, base_url)
    if problems:
        return False, [f"GET {DISCOVERY_PATH} -> 200, but " + "; ".join(problems)]
    status, keys, _ = fetch("GET", oauth_base(base_url) + JWKS_PATH, None)
    if status != 200 or not isinstance(keys, dict) or not keys.get("keys"):
        return False, [f"GET {DISCOVERY_PATH} -> 200, but GET {JWKS_PATH} -> {status} with no keys: "
                       "every access token would be rejected"]
    return True, [f"GET {DISCOVERY_PATH} -> 200: issuer, endpoints, PKCE S256 and a JWKS with "
                  f"{len(keys['keys'])} key(s), as onboarding registers"]


def main(argv: list[str] | None = None, *, environ: Mapping[str, str] | None = None,
         client_factory: Callable[[], Airwallex] | None = None, fetch: Fetch = _urllib_fetch,
         out: TextIO = sys.stdout) -> int:
    env = os.environ if environ is None else environ
    parser = argparse.ArgumentParser(prog="ac_onboard.py", description=__doc__.split("\n\n")[0])
    parser.add_argument("--base-url", default=env.get("COUNTERSIGN_PUBLIC_URL"),
                        help="the public https host that serves the edge (/acp, /p, /img)")
    parser.add_argument("--check-host", action="store_true", help="check the host only, call no Airwallex API")
    parser.add_argument("--onboard", action="store_true", help="POST /onboard once, after the probe and host check")
    parser.add_argument("--update", action="store_true",
                        help="POST /config/update once onboarded: create_checkout, secret, OAuth and image domains")
    parser.add_argument("--skip-host-check", action="store_true", help="send even if the host check fails")
    parser.add_argument("--dry-run", action="store_true", help="with --onboard or --update: print the body, secrets hidden")
    args = parser.parse_args(argv)

    base = (args.base_url or "").rstrip("/")
    if args.onboard and args.update:
        parser.error("--onboard or --update, not both")
    if (args.check_host or args.onboard or args.update) and not base.startswith("https://"):
        parser.error("needs --base-url (or COUNTERSIGN_PUBLIC_URL) as an https URL")

    if args.onboard:
        try:
            body, which = onboard_body(env, base)
        except ValueError as err:
            parser.error(str(err))
        if args.dry_run:
            print(f"POST /api/v1{ONBOARD_PATH} (PSP key: {which}), nothing sent:", file=out)
            print(json.dumps(redacted(body), indent=2), file=out)
            return 0
    if args.update:
        try:
            body = update_body(env, base)
        except ValueError as err:
            parser.error(str(err))
        if args.dry_run:
            print(f"POST /api/v1{UPDATE_PATH}, nothing sent:", file=out)
            print(json.dumps(redacted(body), indent=2), file=out)
            return 0

    if args.check_host:
        ok, lines = check_host(base, fetch)
        print("\n".join(lines), file=out)
        return 0 if ok else 1

    if not (env.get("AIRWALLEX_CLIENT_ID") and env.get("AIRWALLEX_API_KEY")):
        print("no Airwallex keys in the environment (AIRWALLEX_CLIENT_ID, AIRWALLEX_API_KEY): nothing called", file=out)
        return 1
    client = (client_factory or (lambda: Airwallex(env["AIRWALLEX_CLIENT_ID"], env["AIRWALLEX_API_KEY"],
                                                  env.get("AIRWALLEX_BASE_URL", "https://api.sandbox.airwallex.com"))))()

    state, config, lines = probe(client)
    print("\n".join(lines), file=out)
    if state == "login_refused":
        print("Airwallex refused the login: check AIRWALLEX_CLIENT_ID and AIRWALLEX_API_KEY", file=out)
        return 2
    if state == "key_scope":
        print("a 401 on a config path: this key's scope is too narrow. Create a full-access key "
              "(limited keys get \"Admin permissions required\") and probe again", file=out)
        return 2
    if state == "not_live":
        print("onboarding API not on this account: both config paths 404 with no Airwallex code", file=out)
        return 1
    if state == "unclear":
        print("no clear verdict from the two paths above: nothing sent; read the codes", file=out)
        return 1
    if state == "onboarded":
        print("onboarded:", file=out)
        print("\n".join(summary(config)), file=out)
        if args.update:
            return _update(client, body, base, fetch, args.skip_host_check, out)
        if not args.onboard:
            return 0
        current = (config.get("apis") or {}).get("create_checkout")
        if current == create_checkout_url(base):
            print("already onboarded with this create_checkout: nothing sent", file=out)
            return 0
        print(f"create_checkout differs from {create_checkout_url(base)}. A second POST /onboard is ignored; "
              "change it with --update, which sends create_checkout, the secret and OAuth together",
              file=out)
        return 1

    print("onboarding API is live; this account is not onboarded yet", file=out)
    if args.update:
        print("nothing to update: onboard first (--onboard)", file=out)
        return 1
    if not args.onboard:
        return 1
    if not args.skip_host_check:
        ok, lines = check_host(base, fetch)
        print("\n".join(lines), file=out)
        if not ok:
            print("host not ready: nothing sent (Airwallex's first call would fail)", file=out)
            return 1
    try:
        reply = client.post(ONBOARD_PATH, body)
    except AirwallexError as err:
        print(f"Airwallex refused the onboarding: {_refusal(err)}", file=out)
        return 2
    print(f"onboarded (PSP key: {which}):", file=out)
    print("\n".join(summary(reply)), file=out)
    return 0


def _update(client: Airwallex, body: dict, base: str, fetch: Fetch, skip_host_check: bool, out: TextIO) -> int:
    """POST /config/update for an onboarded account, after the host check."""
    if not skip_host_check:
        ok, lines = check_host(base, fetch)
        print("\n".join(lines), file=out)
        if not ok:
            print("host not ready: nothing sent (Airwallex's next call would fail)", file=out)
            return 1
    try:
        reply = client.post(UPDATE_PATH, body)
    except AirwallexError as err:
        print(f"Airwallex refused the update: {_refusal(err)}", file=out)
        return 2
    print("updated (apis and image domains; the PSP keys unchanged):", file=out)
    print("\n".join(summary(reply)), file=out)
    return 0


if __name__ == "__main__":
    sys.exit(main())
