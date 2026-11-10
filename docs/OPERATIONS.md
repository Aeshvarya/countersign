# Operations

Running Countersign against the Airwallex sandbox, its routes and settings, and deploying the public host. To try it offline, the [README quickstart](../README.md#quickstart) is enough. What to type to Claude is in [RUNBOOK.md](RUNBOOK.md).

## Against the Airwallex sandbox

Copy `.env.example` to `.env` and fill it in:

```bash
set -a; . ./.env; set +a
.venv/bin/python -m countersign fx --merchant USD --shopper INR   # lock a real rate
.venv/bin/python scripts/payment_spike.py --shopper INR          # rate lock + payment with the sandbox test card, checked end to end
.venv/bin/python scripts/upload_catalog.py --dry-run             # build and check the 100 catalogue rows, call nothing
.venv/bin/python scripts/upload_catalog.py --base-url https://<public host>   # upsert them to Airwallex
.venv/bin/python scripts/ac_onboard.py                          # is Kit 10 onboarding on this account, are we onboarded
.venv/bin/python scripts/ac_onboard.py --check-host --base-url https://<public host>   # does the host serve /acp/create_checkout, /p and the OAuth server
.venv/bin/python scripts/ac_onboard.py --onboard --base-url https://<public host>      # onboard once (add --dry-run to see the body, secrets hidden)
.venv/bin/python scripts/ac_onboard.py --update --base-url https://<public host>       # change create_checkout, secret and OAuth together (config/update)
.venv/bin/python scripts/smoke.py --base-url https://<public host>       # every door of the deployed host (more with each secret in .env)
.venv/bin/python scripts/smoke.py --base-url https://<public host> --buy # one purchase end to end: you approve and pay, it checks the receipt
.venv/bin/python scripts/change_event.py --base-url https://<public host> --sku <sku> --percent 12   # the price moves on cue (also --price, --stock, --delivery)
.venv/bin/python scripts/change_event.py --base-url https://<public host> --reset                    # every moved product back as uploaded
.venv/bin/python scripts/dispute_demo.py --receipt countersign-receipt-<order>.json                   # a sandbox dispute on that payment, answered with the receipt
eval "$(.venv/bin/python scripts/mcp_token.py)"                          # an Airwallex login token into the shell, never on screen (not used on the Claude Desktop path)
.venv/bin/python scripts/contrast.py                                     # WCAG AA contrast of every colour pair, light and dark (--strict exits 1)
.venv/bin/python scripts/audit.py                                        # before the repo goes public: credentials, .env, card numbers, links
```

## Catalogue, checkout API and passkeys

The upload sends whole products per call (an upsert replaces a product with all its variants) and never uses the file-ingest replace mode. It exits 1 if any row failed. `COUNTERSIGN_PUBLIC_URL` can stand in for `--base-url`; that host must serve the edge's `/p/<sku>` and `/img/<sku>.svg`.

The checkout API Airwallex calls (`create_checkout` = `https://<host>/acp/create_checkout`) and our own ACP sessions (`/acp/checkout_sessions`, what our MCP calls) are mounted when the edge is built with `acp=AcpConfig.from_env(os.environ)`; set `COUNTERSIGN_ACP_HMAC_SECRET` (the secret given to Airwallex at onboarding). Without it the routes do not exist. `COUNTERSIGN_ACP_OBSERVE=1` logs the header names and body keys of a refused call, never values, to learn an unpublished contract from the first call.

With `COUNTERSIGN_REQUIRE_SHOPPER_SIGNATURE=1` (it needs `COUNTERSIGN_PUBLIC_URL`, whose origin and host name the passkey is checked against) a session waits for the shopper: `not_ready_for_payment`, an `approval_required` message and an `approval` link to `/approve/<id>`. The approval page calls `GET /acp/approvals/<id>` (the terms to show, plus `webauthn.challenge`, `rp_id` and `origin`) and `POST /acp/approvals/<id>` with `credential_id`, `public_key_spki_der` (from `getPublicKey()` at registration), `authenticator_data`, `client_data_json` and `signature` from `navigator.credentials.get()`, all base64url. These two routes take no HMAC: the caller is the shopper's browser. The challenge covers the terms, the session and the approval's expiry, and any requote clears the signature.

## The shopper's page and the console

**The shopper's page** (`/approve/<id>`, the same page at `/pay/<id>`, `web/static/shopper.html`) takes the shopper from the agent's request to the receipt: it shows the exact terms and signs them with a passkey (a device's first approval creates one; the page keeps its credential id and public key, and signs the server's challenge bytes, never the terms hash, refetching the challenge first so it never signs terms the shopper has not seen), waits for the agent to complete, mounts Airwallex's Drop-in when the order is a card payment (`GET /api/checkout/<id>` hands it the intent's client secret and the locked quote; the card never touches this service), asks the merchant to check the payment with Airwallex after Drop-in's success (`POST /api/checkout/<id>/confirm`, throttled; the webhook settles it too), and shows the signed receipt with every check run in the browser. The order's `permalink_url` points here, and our MCP gives the agent this link as `pay_url` while a payment is open. `/verify` checks any receipt the same way, in the browser that opens it (a `#r=` link carries the receipt in the fragment, which never reaches a server).

**The merchant console** (`/console#t=<COUNTERSIGN_LEVER_TOKEN>`; the token moves to the tab and leaves the address bar at once) shows each checkout call as it lands, from Airwallex, our MCP, TAP-signed agents and shoppers' passkeys, with the countersign's answer, plus checkouts, orders and receipts. Its levers raise a price, empty the stock or slow the delivery of the product in the latest checkout; a script calls the same functions at `POST /api/levers/price|stock|delivery|reset` with the same bearer token (`web/levers.py`, `scripts/change_event.py`: exit 1 if the host changed but Airwallex's catalogue did not). With `COUNTERSIGN_SYNC_CATALOG=1` a lever also upserts the product, all its variants in one call, to Airwallex's catalogue. The feed reads response status and our own response JSON, never a request body. The host serves that page at `/approve/<id>` (`web/static/shopper.html`): it creates a passkey on a device's first approval, keeps its credential id and public key in the browser, and signs the server's challenge bytes, never the terms hash.

## Onboarding

The probe is read-only and asks both config paths (`/pa/agentic_commerce/config`, documented, and `/agentic-commerce/config`, where another team saw a 401 with a scoped key): a 401 on either means the key needs full access, not that onboarding is missing. Onboarding happens once: Airwallex ignores a second `POST /onboard`. `ac_onboard.py --onboard` first checks that the host answers an unsigned call from the `/acp` door (not the TAP door, not a 404) and sends nothing if it does not. It reads the PSP key from `AIRWALLEX_PSP_CLIENT_ID` and `AIRWALLEX_PSP_API_KEY`, or the login key if those are empty. It exits 1 when something is not ready, 2 when Airwallex refuses.

## Deploy

Render → New → Blueprint → this repository (`render.yaml`). The one service is the whole public host (`src/countersign/web/host.py`): the demo page, plus one long-lived merchant behind the real edge that serves `/acp/create_checkout` (Airwallex's `create_checkout`), `/acp/checkout_sessions` (our MCP's ACP door), `/p/<sku>` and `/img/<sku>.svg` (the catalogue links), the TAP door (`/products`, `/offers`, `/checkout`, `/orders/<key>`) and `/.well-known/countersign-merchant-key` (the key that verifies its receipts). Set in the dashboard: `COUNTERSIGN_PUBLIC_URL` (the service's https URL, also the TAP authority), `COUNTERSIGN_ACP_HMAC_SECRET`, `COUNTERSIGN_MERCHANT_SIGNING_KEY` (the private line of `python -m countersign keygen`, so receipts verify across restarts) and the Airwallex keys (real rate locks). `COUNTERSIGN_MERCHANT=airwallex` makes the house merchant take real card payments (intent, Drop-in, then `/webhooks/airwallex` settles it under `AIRWALLEX_WEBHOOK_SECRET` and only then signs the receipt); the default pays instantly, offline. `COUNTERSIGN_MCP_TOKEN` also serves our own merchant MCP at `/acp/mcp` (`web/merchant_mcp.py`): the fallback for when Airwallex's merchant MCP is not available, which calls `/acp` with the HMAC secret exactly as Airwallex does. Connect it with `claude mcp add --transport http countersign <url>/acp/mcp --header "Authorization: Bearer <token>"`. `COUNTERSIGN_LEVER_TOKEN` (32+ characters, different from the MCP token and the HMAC secret) turns on the merchant console and the lever route. `/healthz` says what is switched on. After a deploy, run `scripts/smoke.py --base-url <url>` and keep an uptime monitor on `/healthz` every 5 minutes: the house keeps checkout sessions in memory, so a sleep forgets them. Set `ANTHROPIC_API_KEY` to run Claude as the demo agent.
