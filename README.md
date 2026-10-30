# Countersign

A merchant checkout that countersigns a verified agent and the shopper's approved terms before any money moves.

<!-- Pitch, why it matters, prior art, team: owner's words go here (WORKPLAN V8). -->

## Status

Build phase (2026-10-27 to 2026-11-13). The product runs offline end to end: approvals bound to the terms a shopper saw, the decision that charges or asks again, Visa Trusted Agent Protocol signing and verification, rate locks, and a 100-product synthetic merchant.

| Piece | State |
|---|---|
| Catalogue for Airwallex's merchant MCP, in Google Merchant Center format (C11) | done offline; live upload waits on sandbox keys and Kit 10 onboarding |
| Public product pages and images the catalogue links to | done |
| Checkout API Airwallex calls (`create_checkout`, C10), on ACP 2026-04-17 | done offline; onboarding script ready; live run waits on sandbox keys, a public host and Kit 10 onboarding |
| Real card payments through Airwallex (C9) | code written; first live run waits on sandbox keys |
| Shopper passkey approval (U1, C13) | planned |

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
```

The upload sends whole products per call (an upsert replaces a product with all its variants) and never uses the file-ingest replace mode. It exits 1 if any row failed. `COUNTERSIGN_PUBLIC_URL` can stand in for `--base-url`; that host must serve the edge's `/p/<sku>` and `/img/<sku>.svg`.

The checkout API Airwallex calls (`create_checkout` = `https://<host>/acp/checkout_sessions`) is mounted when the edge is built with `acp=AcpConfig.from_env(os.environ)`; set `COUNTERSIGN_ACP_HMAC_SECRET` (the secret given to Airwallex at onboarding). Without it the routes do not exist. `COUNTERSIGN_ACP_OBSERVE=1` logs the header names and body keys of a refused call, never values, to learn an unpublished contract from the first call.

Onboarding happens once: Airwallex ignores a second `POST /onboard`. `ac_onboard.py --onboard` first checks that the host answers an unsigned call from the `/acp` door (not the TAP door, not a 404) and sends nothing if it does not. It reads the PSP key from `AIRWALLEX_PSP_CLIENT_ID` and `AIRWALLEX_PSP_API_KEY`, or the login key if those are empty. It exits 1 when something is not ready, 2 when Airwallex refuses.

Deploy the demo: Render → New → Blueprint → this repository (`render.yaml`). Set `ANTHROPIC_API_KEY` there to run Claude as the agent.

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
| `src/countersign/web/` | the demo app |
| `src/countersign/payments.py` | the card payment: Airwallex intent, Drop-in handoff, server-side check |
| `src/countersign/edge/` | the merchant's HTTP API behind the TAP check, plus the public product pages (`edge/pages.py`) |
| `src/countersign/edge/acp.py` | C10 the checkout API Airwallex calls: sessions, complete runs the countersign, REASK messages |
| `src/countersign/edge/hmac_auth.py`, `edge/idempotency.py` | the platform HMAC check (one scheme by config) and ACP idempotency rules |
| `src/countersign/catalog_gmc.py` | C11 the catalogue as GMC rows, chunking that keeps a product's variants together, the upload |
| `src/countersign/airwallex.py` | Airwallex REST client |
| `scripts/payment_spike.py` | real rate lock + real payment on the Airwallex sandbox, in one command |
| `scripts/tap_interop.py` | interop check against Visa's TAP reference code |
| `scripts/upload_catalog.py` | upload the catalogue to Airwallex (dry run without keys) |
| `scripts/ac_onboard.py` | onboarding probe, host check, and the one-time Kit 10 onboarding |
| `PROGRESS.md`, `WORKPLAN.md` | where the work stands, who does what |

## Licence

Apache 2.0.
