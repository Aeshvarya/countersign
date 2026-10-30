"""V10 live: onboard the merchant with Airwallex once, and check everything first (C10).

    set -a; . ./.env; set +a
    PYTHONPATH=src python scripts/ac_onboard.py                      # probe only
    PYTHONPATH=src python scripts/ac_onboard.py --check-host         # is /acp served?
    PYTHONPATH=src python scripts/ac_onboard.py --onboard --dry-run  # the body, secrets hidden
    PYTHONPATH=src python scripts/ac_onboard.py --onboard            # POST /onboard, once

Probe: GET /api/v1/pa/agentic_commerce/config tells three states apart. 200 means
onboarded. 404 MERCHANT_CONFIG_NOT_FOUND means the API is live and we have not
onboarded. Any other 404 means the route itself is missing on this account.

Host check: an unsigned POST to create_checkout must be refused by the ACP door
(401, code "unauthorized"). A TAP refusal ({"refused": ...}) means the host runs
the edge without COUNTERSIGN_ACP_HMAC_SECRET, so the /acp routes are not
mounted; a 404 means the host does not serve the edge at all. Either way
Airwallex's first call would fail, so --onboard sends nothing.

Onboarding is one-shot: Airwallex ignores the payload of a second POST /onboard,
and changes go through PUT /config, which replaces a whole section (the PSP key
pair, or the URL and secret together). This script does not send PUT.

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

from countersign import catalog_gmc
from countersign.airwallex import Airwallex, AirwallexError
from countersign.edge.acp import PREFIX, AcpConfig

CONFIG_PATH = "/pa/agentic_commerce/config"
ONBOARD_PATH = "/pa/agentic_commerce/onboard"
NOT_ONBOARDED = "MERCHANT_CONFIG_NOT_FOUND"
SLOW_SECONDS = 10.0  # a free Render instance waking from sleep takes longer than this

# fetch(method, url, body) -> (status, parsed JSON body or None, seconds taken)
Fetch = Callable[[str, str, bytes | None], tuple[int, object, float]]


def _code(err: AirwallexError) -> str:
    body = err.body if isinstance(err.body, dict) else {}
    return str(body.get("code") or "")


def _refusal(err: AirwallexError) -> str:
    body = err.body if isinstance(err.body, dict) else {}
    return " ".join([f"HTTP {err.status}"] + [str(body[k]) for k in ("code", "message") if body.get(k)])


def probe(client: Airwallex) -> tuple[str, dict | None, AirwallexError | None]:
    """("onboarded", config), ("not_onboarded", None), ("not_live", err) or ("refused", err)."""
    try:
        return "onboarded", client.get(CONFIG_PATH), None
    except AirwallexError as err:
        if err.status == 404 and _code(err).upper() == NOT_ONBOARDED:
            return "not_onboarded", None, None
        if err.status == 404:
            return "not_live", None, err
        return "refused", None, err


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


def onboard_body(env: Mapping[str, str], base_url: str) -> tuple[dict, str]:
    """The first POST /onboard body: both sections, all four values. ValueError if one is missing."""
    acp = AcpConfig.from_env(env)  # the same 32-byte rule the edge applies at start-up
    if acp is None:
        raise ValueError("COUNTERSIGN_ACP_HMAC_SECRET is not set")
    client_id, api_key, which = psp_keys(env)
    if not (client_id and api_key):
        raise ValueError("no PSP key: set AIRWALLEX_CLIENT_ID and AIRWALLEX_API_KEY")
    body = {
        "psp_config": {"api_key_data": {"client_id": client_id, "api_key": api_key}},
        "apis": {"create_checkout": create_checkout_url(base_url), "secret_key": acp.secret.decode()},
    }
    return body, which


def redacted(body: dict) -> dict:
    hidden = json.loads(json.dumps(body))
    data = hidden["psp_config"]["api_key_data"]
    data["api_key"] = f"<set, {len(data['api_key'])} chars>"
    hidden["apis"]["secret_key"] = f"<set, {len(hidden['apis']['secret_key'])} chars>"
    return hidden


def create_checkout_url(base_url: str) -> str:
    return base_url.rstrip("/") + PREFIX


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
        lines.append(f"POST {PREFIX} unsigned -> 401 from the ACP door ({timing})")
    elif isinstance(body, dict) and "refused" in body:
        ok = False
        lines.append(f"POST {PREFIX} unsigned -> {status} from the TAP door: the host has no "
                     f"COUNTERSIGN_ACP_HMAC_SECRET, so /acp is not mounted ({timing})")
    else:
        ok = False
        lines.append(f"POST {PREFIX} unsigned -> {status}: the host does not serve the edge's /acp routes ({timing})")

    sku = sku or catalog_gmc.catalog(base_url)[0]["id"]
    status, _, seconds = fetch("GET", f"{base_url.rstrip('/')}/p/{sku}", None)
    if status == 200:
        lines.append(f"GET /p/{sku} -> 200 ({seconds:.1f} s)")
    else:
        ok = False
        lines.append(f"GET /p/{sku} -> {status}: catalogue links would not resolve")
    return ok, lines


def main(argv: list[str] | None = None, *, environ: Mapping[str, str] | None = None,
         client_factory: Callable[[], Airwallex] | None = None, fetch: Fetch = _urllib_fetch,
         out: TextIO = sys.stdout) -> int:
    env = os.environ if environ is None else environ
    parser = argparse.ArgumentParser(prog="ac_onboard.py", description=__doc__.split("\n\n")[0])
    parser.add_argument("--base-url", default=env.get("COUNTERSIGN_PUBLIC_URL"),
                        help="the public https host that serves the edge (/acp, /p, /img)")
    parser.add_argument("--check-host", action="store_true", help="check the host only, call no Airwallex API")
    parser.add_argument("--onboard", action="store_true", help="POST /onboard once, after the probe and host check")
    parser.add_argument("--skip-host-check", action="store_true", help="onboard even if the host check fails")
    parser.add_argument("--dry-run", action="store_true", help="with --onboard: print the body, secrets hidden")
    args = parser.parse_args(argv)

    base = (args.base_url or "").rstrip("/")
    if (args.check_host or args.onboard) and not base.startswith("https://"):
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

    if args.check_host:
        ok, lines = check_host(base, fetch)
        print("\n".join(lines), file=out)
        return 0 if ok else 1

    if not (env.get("AIRWALLEX_CLIENT_ID") and env.get("AIRWALLEX_API_KEY")):
        print("no Airwallex keys in the environment (AIRWALLEX_CLIENT_ID, AIRWALLEX_API_KEY): nothing called", file=out)
        return 1
    client = (client_factory or (lambda: Airwallex(env["AIRWALLEX_CLIENT_ID"], env["AIRWALLEX_API_KEY"],
                                                  env.get("AIRWALLEX_BASE_URL", "https://api.sandbox.airwallex.com"))))()

    state, config, err = probe(client)
    if state == "not_live":
        print(f"onboarding API not on this account: GET {CONFIG_PATH} -> {_refusal(err)}", file=out)
        return 1
    if state == "refused":
        print(f"Airwallex refused the probe: {_refusal(err)}", file=out)
        return 2
    if state == "onboarded":
        print("onboarded:", file=out)
        print("\n".join(summary(config)), file=out)
        if not args.onboard:
            return 0
        current = (config.get("apis") or {}).get("create_checkout")
        if current == create_checkout_url(base):
            print("already onboarded with this create_checkout: nothing sent", file=out)
            return 0
        print(f"create_checkout differs from {create_checkout_url(base)}. A second POST /onboard is ignored; "
              "change it with PUT /config, sending the URL and the secret together", file=out)
        return 1

    print("onboarding API is live; this account is not onboarded yet", file=out)
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


if __name__ == "__main__":
    sys.exit(main())
