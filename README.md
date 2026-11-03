# Countersign

A merchant checkout that countersigns a verified agent and the shopper's approved terms before any money moves.

<!-- Pitch, why it matters, prior art, team: owner's words go here (WORKPLAN V8). -->

## Status

Build phase (2026-10-27 to 2026-11-13). The product runs offline end to end: approvals bound to the terms a shopper saw, the decision that charges or asks again, Visa Trusted Agent Protocol signing and verification, rate locks, and a 100-product synthetic merchant.

| Piece | State |
|---|---|
| Catalogue for Airwallex's merchant MCP, in Google Merchant Center format (C11) | done offline; live upload waits on sandbox keys and Kit 10 onboarding |
| Public product pages and images the catalogue links to | done |
| Airwallex's `create_checkout` (C10 amended, guide of 2026-11-03), `/acp/create_checkout` | done offline: every call prices the cart from the live catalogue with a fresh rate lock and keeps the quote for the payment webhook; a line we cannot fill comes back with `available_quantity` 0, never an error; one SHIPPING method, standard free and express 9.99 USD at the quote's rate; the total never includes shipping; onboarding registers the new URL once W4 lands |
| Our ACP checkout sessions (`/acp/checkout_sessions`, C10), on ACP 2026-04-17, for our own MCP | done offline; the shopper's ceiling holds: a re-quote above it waits for a new ceiling or, with U1, the shopper's passkey on the new terms, and a currency change waits for a ceiling in the new currency; a currency it cannot price is refused with the reason, and an Airwallex outage is a 503 to retry, never reported as the shopper's currency; the shipping address is kept and returned (not yet in the receipt); a card order's receipt shows in the session once Airwallex confirms the payment; onboarding script ready; live run waits on sandbox keys, a public host and Kit 10 onboarding |
| Claude as the shopper's agent (A13) | done on our fallback MCP: `docs/RUNBOOK.md` (clean profile, the add commands, a purchase prompt and each exception, rehearsed five times); `scripts/mcp_token.py` puts Airwallex's MCP token in the shell; Airwallex's MCP waits on Kit 10 onboarding |
| Real card payments through Airwallex (C9) | done offline: the shopper pays in Drop-in on `/pay/<id>`, paid only from Airwallex's own record; a refused card leaves the order open for another card and says what Airwallex reported, and cancel closes the payment; first live run waits on sandbox keys |
| Shopper passkey approval (U1, C13) | done: verifier, edge routes, the shopper page (`/approve/<id>`), and the signature in the receipt (C13), attached only on `/acp` after it checks, never taken from an agent on the TAP door. It proves a passkey for this site signed the exact terms, not whose passkey it is (Decisions 2026-10-31) |
| Receipts anyone can check (C8) | done: the shopper page and `/verify` check the merchant's signature in the browser, no server asked |
| Merchant console and levers (A15, V6) | done: `/console` shows every checkout call and the countersign's answer live; price, stock and delivery change on cue, and a reset puts the catalogue back for the next take; `scripts/change_event.py` does the same from a terminal |
| Colours (V8) | one token file, light and dark, with the palette and a type, space, shape and motion scale (`edge/static/tokens.css`); the catalogue pages and product images use it; the web pages move onto it on `varunika/v8-apply-web` (in review); `scripts/contrast.py` checks every pair against WCAG AA (all pass) |
| Deploy checks (A7, A10) | `scripts/smoke.py` checks every door of the deployed host; `scripts/audit.py` (also in CI) keeps credentials, real card numbers and dead links out of the public repo |

## Quickstart

```bash
uv venv && uv pip install -e '.[dev]'
.venv/bin/python -m countersign demo                    # the contrast pairs and a dispute, offline, no keys
.venv/bin/uvicorn countersign.web.app:app --port 8000   # the demo app: http://127.0.0.1:8000
.venv/bin/python -m unittest discover -s tests          # tests, offline
```

The app uses Claude when `ANTHROPIC_API_KEY` is set, and a scripted stand-in otherwise (`COUNTERSIGN_MODEL=scripted` forces it). Open `/?q=Buy+me+a+kettle` to start straight away, or `/?s=<session id>` to watch a running session from a second screen.

Against the Airwallex sandbox (copy `.env.example` to `.env` and fill it in):

```bash
set -a; . ./.env; set +a
.venv/bin/python -m countersign fx --merchant USD --shopper INR   # lock a real rate
.venv/bin/python scripts/payment_spike.py --shopper INR          # rate lock + payment with the sandbox test card, checked end to end
.venv/bin/python scripts/upload_catalog.py --dry-run             # build and check the 100 catalogue rows, call nothing
.venv/bin/python scripts/upload_catalog.py --base-url https://<public host>   # upsert them to Airwallex
.venv/bin/python scripts/ac_onboard.py                          # is Kit 10 onboarding on this account, are we onboarded
.venv/bin/python scripts/ac_onboard.py --check-host --base-url https://<public host>   # does the host serve /acp and /p
.venv/bin/python scripts/ac_onboard.py --onboard --base-url https://<public host>      # onboard once (add --dry-run to see the body, secrets hidden)
.venv/bin/python scripts/smoke.py --base-url https://<public host>       # every door of the deployed host (more with each secret in .env)
.venv/bin/python scripts/smoke.py --base-url https://<public host> --buy # one purchase end to end: you approve and pay, it checks the receipt
.venv/bin/python scripts/change_event.py --base-url https://<public host> --sku <sku> --percent 12   # the price moves on cue (also --price, --stock, --delivery)
.venv/bin/python scripts/change_event.py --base-url https://<public host> --reset                    # every moved product back as uploaded
eval "$(.venv/bin/python scripts/mcp_token.py)"                          # Airwallex's merchant MCP token into the shell, never on screen (docs/RUNBOOK.md)
.venv/bin/python scripts/contrast.py                                     # WCAG AA contrast of every colour pair, light and dark (--strict exits 1)
.venv/bin/python scripts/audit.py                                        # before the repo goes public: credentials, .env, card numbers, links
```

The upload sends whole products per call (an upsert replaces a product with all its variants) and never uses the file-ingest replace mode. It exits 1 if any row failed. `COUNTERSIGN_PUBLIC_URL` can stand in for `--base-url`; that host must serve the edge's `/p/<sku>` and `/img/<sku>.svg`.

The checkout API Airwallex calls (`create_checkout` = `https://<host>/acp/create_checkout`) and our own ACP sessions (`/acp/checkout_sessions`, what our MCP calls) are mounted when the edge is built with `acp=AcpConfig.from_env(os.environ)`; set `COUNTERSIGN_ACP_HMAC_SECRET` (the secret given to Airwallex at onboarding). Without it the routes do not exist. `COUNTERSIGN_ACP_OBSERVE=1` logs the header names and body keys of a refused call, never values, to learn an unpublished contract from the first call.

With `COUNTERSIGN_REQUIRE_SHOPPER_SIGNATURE=1` (it needs `COUNTERSIGN_PUBLIC_URL`, whose origin and host name the passkey is checked against) a session waits for the shopper: `not_ready_for_payment`, an `approval_required` message and an `approval` link to `/approve/<id>`. The approval page calls `GET /acp/approvals/<id>` (the terms to show, plus `webauthn.challenge`, `rp_id` and `origin`) and `POST /acp/approvals/<id>` with `credential_id`, `public_key_spki_der` (from `getPublicKey()` at registration), `authenticator_data`, `client_data_json` and `signature` from `navigator.credentials.get()`, all base64url. These two routes take no HMAC: the caller is the shopper's browser. The challenge covers the terms, the session and the approval's expiry, and any requote clears the signature.

**The shopper's page** (`/approve/<id>`, the same page at `/pay/<id>`, `web/static/shopper.html`) takes the shopper from the agent's request to the receipt: it shows the exact terms and signs them with a passkey (a device's first approval creates one; the page keeps its credential id and public key, and signs the server's challenge bytes, never the terms hash, refetching the challenge first so it never signs terms the shopper has not seen), waits for the agent to complete, mounts Airwallex's Drop-in when the order is a card payment (`GET /api/checkout/<id>` hands it the intent's client secret and the locked quote; the card never touches this service), asks the merchant to check the payment with Airwallex after Drop-in's success (`POST /api/checkout/<id>/confirm`, throttled; the webhook settles it too), and shows the signed receipt with every check run in the browser. The order's `permalink_url` points here, and our MCP gives the agent this link as `pay_url` while a payment is open. `/verify` checks any receipt the same way, in the browser that opens it (a `#r=` link carries the receipt in the fragment, which never reaches a server).

**The merchant console** (`/console#t=<COUNTERSIGN_LEVER_TOKEN>`; the token moves to the tab and leaves the address bar at once) shows each checkout call as it lands, from Airwallex, our MCP, TAP-signed agents and shoppers' passkeys, with the countersign's answer, plus checkouts, orders and receipts. Its levers raise a price, empty the stock or slow the delivery of the product in the latest checkout; a script calls the same functions at `POST /api/levers/price|stock|delivery|reset` with the same bearer token (`web/levers.py`, `scripts/change_event.py`: exit 1 if the host changed but Airwallex's catalogue did not). With `COUNTERSIGN_SYNC_CATALOG=1` a lever also upserts the product, all its variants in one call, to Airwallex's catalogue. The feed reads response status and our own response JSON, never a request body. The host serves that page at `/approve/<id>` (`web/static/approve.html`): it creates a passkey on a device's first approval, keeps its credential id and public key in the browser, and signs the server's challenge bytes, never the terms hash.

The probe is read-only and asks both config paths (`/pa/agentic_commerce/config`, documented, and `/agentic-commerce/config`, where another team saw a 401 with a scoped key): a 401 on either means the key needs full access, not that onboarding is missing. Onboarding happens once: Airwallex ignores a second `POST /onboard`. `ac_onboard.py --onboard` first checks that the host answers an unsigned call from the `/acp` door (not the TAP door, not a 404) and sends nothing if it does not. It reads the PSP key from `AIRWALLEX_PSP_CLIENT_ID` and `AIRWALLEX_PSP_API_KEY`, or the login key if those are empty. It exits 1 when something is not ready, 2 when Airwallex refuses.

Deploy: Render → New → Blueprint → this repository (`render.yaml`). The one service is the whole public host (`src/countersign/web/host.py`): the demo page, plus one long-lived merchant behind the real edge that serves `/acp/create_checkout` (Airwallex's `create_checkout`), `/acp/checkout_sessions` (our MCP's ACP door), `/p/<sku>` and `/img/<sku>.svg` (the catalogue links), the TAP door (`/products`, `/offers`, `/checkout`, `/orders/<key>`) and `/.well-known/countersign-merchant-key` (the key that verifies its receipts). Set in the dashboard: `COUNTERSIGN_PUBLIC_URL` (the service's https URL, also the TAP authority), `COUNTERSIGN_ACP_HMAC_SECRET`, `COUNTERSIGN_MERCHANT_SIGNING_KEY` (the private line of `python -m countersign keygen`, so receipts verify across restarts) and the Airwallex keys (real rate locks). `COUNTERSIGN_MERCHANT=airwallex` makes the house merchant take real card payments (intent, Drop-in, then `/webhooks/airwallex` settles it under `AIRWALLEX_WEBHOOK_SECRET` and only then signs the receipt); the default pays instantly, offline. `COUNTERSIGN_MCP_TOKEN` also serves our own merchant MCP at `/acp/mcp` (`web/merchant_mcp.py`): the fallback for when Airwallex's merchant MCP is not available, which calls `/acp` with the HMAC secret exactly as Airwallex does. Connect it with `claude mcp add --transport http countersign <url>/acp/mcp --header "Authorization: Bearer <token>"`. `COUNTERSIGN_LEVER_TOKEN` (32+ characters, different from the MCP token and the HMAC secret) turns on the merchant console and the lever route. `/healthz` says what is switched on. After a deploy, run `scripts/smoke.py --base-url <url>` and keep an uptime monitor on `/healthz` every 5 minutes: the house keeps checkout sessions in memory, so a sleep forgets them. Set `ANTHROPIC_API_KEY` to run Claude as the demo agent.

## How it works

1. **The agent proves who it is.** Every request to the merchant carries a Visa Trusted Agent Protocol signature (Ed25519, bound to the merchant's host and the exact path, with a nonce and a validity window). The merchant's edge refuses unsigned, expired, replayed or misdirected requests before anything else runs. `src/countersign/tap.py`
2. **The shopper approves exact terms.** Merchant, items, delivery, totals and a ceiling in the shopper's own currency, hashed. `src/countersign/terms.py`
3. **The merchant countersigns before it charges.** It rebuilds the live offer with a fresh rate lock and runs one deterministic function: proceed, ask again, or refuse. No model output reaches it. `src/countersign/decide.py`, `src/countersign/checkout.py`
4. **The charge happens in the shopper's currency at the locked rate**, through Airwallex Multi-Currency Pricing, and returns the merchant's order number. `src/countersign/fx.py`, `src/countersign/merchant.py`
5. **The merchant signs a receipt** binding the agent's identity, the approval's hash and ceiling, the charge and the order number. Anyone with the merchant's public key can check it later, offline: `python -m countersign verify-receipt`. `src/countersign/receipt.py`

TAP interop with Visa's reference implementation is checked in both directions by `scripts/tap_interop.py`.

## Layout

| Path | What |
|---|---|
| `src/countersign/terms.py` | C1 terms, approval, canonical hash |
| `src/countersign/tap.py` | C2 TAP sign and verify, key directory, nonce cache |
| `src/countersign/merchant.py` | C3 merchant interface, synthetic catalog, offline merchant |
| `src/countersign/fx.py` | C4 rate locks: Airwallex conversion quotes, offline fake |
| `src/countersign/decide.py` | C5 the decision |
| `src/countersign/checkout.py` | C6 the countersign step |
| `src/countersign/receipt.py` | C8 the countersigned receipt |
| `src/countersign/agent/` | the shopping agent: tool loop, approval pause, TAP-signed shop clients |
| `src/countersign/web/` | the demo app; `web/host.py` makes it the one public host (house merchant, `/acp`, catalogue pages, TAP door); `web/merchant_mcp.py` is our fallback merchant MCP over `/acp` |
| `src/countersign/web/shopper.py`, `web/static/shopper.*` | the shopper's page: passkey approval, Drop-in payment, the receipt |
| `src/countersign/web/static/receipt.js`, `web/static/verify.*` | receipt checks in the browser (C8, C13), and `/verify` |
| `src/countersign/web/console.py`, `web/levers.py`, `web/static/console.*` | A15 the merchant console and its live feed; V6 the levers |
| `src/countersign/web/public.py` | the host's public routes: health check, receipt key, the pages and their APIs |
| `src/countersign/payments.py` | the card payment: Airwallex intent, Drop-in handoff, server-side check |
| `src/countersign/airwallex_merchant.py` | V5′ the merchant on Airwallex: pending orders, confirm from Airwallex's own record, refund on mismatch |
| `src/countersign/edge/` | the merchant's HTTP API behind the TAP check, plus the public product pages (`edge/pages.py`) |
| `src/countersign/edge/static/tokens.css`, `edge/tokens.py` | V8 the colour tokens, light and dark, and their reader |
| `src/countersign/edge/acp.py` | C10 the checkout API: Airwallex's `create_checkout` (a fresh quote per call, kept for the payment webhook), and our ACP sessions, where complete runs the countersign and REASK messages |
| `src/countersign/edge/passkey.py` | U1 the shopper's passkey: the approval challenge and the six-check WebAuthn verifier |
| `src/countersign/edge/hmac_auth.py`, `edge/idempotency.py` | the platform HMAC check (one scheme by config) and ACP idempotency rules |
| `src/countersign/catalog_gmc.py` | C11 the catalogue as GMC rows, chunking that keeps a product's variants together, the upload |
| `src/countersign/airwallex.py` | Airwallex REST client |
| `scripts/payment_spike.py` | real rate lock + real payment on the Airwallex sandbox, in one command |
| `scripts/tap_interop.py` | interop check against Visa's TAP reference code |
| `scripts/upload_catalog.py` | upload the catalogue to Airwallex (dry run without keys) |
| `scripts/ac_onboard.py` | onboarding probe, host check, and the one-time Kit 10 onboarding |
| `scripts/smoke.py` | checks every door of the deployed host; `--buy` runs one purchase end to end |
| `scripts/contrast.py` | V8 WCAG AA contrast of every colour pair the pages use, light and dark |
| `scripts/change_event.py` | V6 from a terminal: price, stock, delivery or reset on the running host, through its lever routes |
| `scripts/mcp_token.py` | A13 the bearer token for Airwallex's merchant MCP, straight into the shell |
| `scripts/audit.py` | the audit before the repo goes public (A10), also run in CI |
| `docs/VIDEO_RUN.md` | the video, shot by shot: what is on screen and what to type (A9) |
| `docs/RUNBOOK.md` | A13 what to type to Claude and what should come back: connecting the MCP, the purchase, each exception |
| `PROGRESS.md`, `WORKPLAN.md` | where the work stands, who does what |

## Licence

Apache 2.0.
